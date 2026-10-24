"""In-memory summary index of stored series, one per collection.

The dashboard lists, filters and pages through every stored series, but each
stored document also carries its full chapter list - most of its weight, and
needed only on the detail page. So the index reads a collection once with a
projection that leaves chapters out, keeps the result in memory, and serves
every page, filter and count from there. Firestore has no substring search,
so the name filter has to happen here regardless.

The app runs as a single process (gunicorn -w 1), so this copy is the only
copy. A scrape invalidates its collection straight away; the TTL is only for
writes made from outside this process.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.models.vortexscans import SeriesSummary
from app.services.firebase import firebase_service

logger = get_logger(__name__)

CACHE_TTL = 600  # seconds

SUMMARY_FIELDS = list(SeriesSummary.model_fields)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _ts(value) -> datetime:
    """Stored timestamps are ISO strings; missing ones sort as oldest."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _EPOCH


def month_key(value) -> str:
    """'2026-08' for a stored timestamp, or '' when missing."""
    ts = _ts(value)
    return "" if ts == _EPOCH else ts.strftime("%Y-%m")


# Each key sorts descending except name. The slug tie-break keeps the order
# total, so a page boundary never lands differently between two requests.
SORTS = {
    "newest": lambda s: (_ts(s.get("first_chapter_released_at")), s.get("slug", "")),
    "updated": lambda s: (_ts(s.get("last_updated_at")), s.get("slug", "")),
    "chapters": lambda s: (s.get("total_chapters") or 0, s.get("slug", "")),
    "name": lambda s: ((s.get("name") or "").casefold(), s.get("slug", "")),
}


class SeriesIndex:
    def __init__(self) -> None:
        self._items: Dict[str, List[dict]] = {}
        self._loaded_at: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _fresh(self, collection: str) -> bool:
        loaded = self._loaded_at.get(collection)
        return loaded is not None and time.monotonic() - loaded < CACHE_TTL

    async def get(self, collection: str) -> List[dict]:
        """Summaries for every series in a collection, loading them if stale."""
        if self._fresh(collection):
            return self._items[collection]
        lock = self._locks.setdefault(collection, asyncio.Lock())
        async with lock:
            # The feed and the status bar ask at the same moment on page load;
            # whoever waited on the lock gets the load the other just did.
            if self._fresh(collection):
                return self._items[collection]
            started = time.monotonic()
            docs = await run_in_threadpool(
                firebase_service.list_projected, collection, SUMMARY_FIELDS
            )
            self._items[collection] = docs
            self._loaded_at[collection] = time.monotonic()
            logger.info(
                f"Indexed {len(docs)} series from {collection} "
                f"in {time.monotonic() - started:.2f}s"
            )
            return docs

    def invalidate(self, collection: str) -> None:
        self._loaded_at.pop(collection, None)

    async def warm(self, collections: Iterable[str]) -> None:
        """Load every collection up front so the first visitor doesn't wait."""
        for collection in collections:
            try:
                await self.get(collection)
            except Exception as e:
                logger.error(f"Could not pre-load {collection}: {e}")


def query_feed(
    items: List[dict],
    *,
    sort: str,
    month: Optional[str],
    q: str,
    offset: int,
    limit: int,
) -> dict:
    """Filter, sort and slice one page, with the month counts the filters show.

    Month counts cover every stored series, not just the filtered ones, so
    the month buttons keep their numbers while a name filter is typed.
    """
    counts: Dict[str, int] = {}
    for s in items:
        key = month_key(s.get("first_chapter_released_at"))
        if key:
            counts[key] = counts.get(key, 0) + 1

    matched = items
    if month:
        matched = [s for s in matched if month_key(s.get("first_chapter_released_at")) == month]
    needle = q.strip().casefold()
    if needle:
        matched = [s for s in matched if needle in (s.get("name") or "").casefold()]

    matched = sorted(matched, key=SORTS[sort], reverse=sort != "name")
    page = matched[offset : offset + limit]
    end = offset + len(page)

    return {
        "items": page,
        "total": len(matched),
        "offset": offset,
        "next_offset": end if end < len(matched) else None,
        "all_count": len(items),
        "months": [
            {"month": m, "count": c}
            for m, c in sorted(counts.items(), reverse=True)
        ],
    }


series_index = SeriesIndex()
