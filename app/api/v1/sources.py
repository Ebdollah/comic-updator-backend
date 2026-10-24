from collections import OrderedDict
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.core.scheduler import get_scheduler_status
from app.models.vortexscans import FeedPage, MultiScrapeResult, ScrapeResult, VortexSeries
from app.services.firebase import firebase_service
from app.services.series_index import query_feed, series_index
from app.services.sources import get_source, list_sources, run_source, scrape_all

logger = get_logger(__name__)
router = APIRouter()

SourcePath = Path(..., description="Source id, e.g. 'vortexscans' or 'thunderscans'")


def _resolve(source: str):
    try:
        return get_source(source)
    except KeyError:
        known = ", ".join(s["id"] for s in list_sources())
        raise HTTPException(404, f"Unknown source '{source}'. Available: {known}")


@router.get("")
async def get_sources() -> List[dict]:
    """List the sources this backend can scrape. Drives the dashboard tabs."""
    return list_sources()


@router.post("/scrape-all", response_model=MultiScrapeResult)
async def scrape_all_sources() -> MultiScrapeResult:
    """Scrape every source at once and report what each one added.

    A source that fails is reported as such; the others still run and save.
    """
    return await scrape_all()


@router.post("/{source}/scrape", response_model=ScrapeResult)
async def scrape_source(
    source: str = SourcePath,
    year: Optional[int] = Query(None, description="Defaults to the current UTC year"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Defaults to now"),
    dry_run: bool = Query(False, description="Parse and filter, but write nothing"),
) -> ScrapeResult:
    """Scrape one source now. Series already stored are skipped, never rewritten."""
    src = _resolve(source)
    try:
        if year is None and month is None and not dry_run:
            return await run_source(src)
        result = await src.scraper.run_and_store(year=year, month=month, dry_run=dry_run)
        if not dry_run:
            series_index.invalidate(src.collection)
        return result
    except Exception as e:
        logger.error(f"Scrape failed for {source}: {e}")
        raise HTTPException(502, f"Scrape failed: {e}")


@router.get("/{source}/feed", response_model=FeedPage)
async def source_feed(
    source: str = SourcePath,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    sort: Literal["newest", "updated", "chapters", "name"] = Query("newest"),
    month: Optional[str] = Query(
        None, pattern=r"^\d{4}-\d{2}$", description="Debut month 'YYYY-MM'; omit for all"
    ),
    q: str = Query("", max_length=200, description="Case-insensitive name filter"),
) -> Dict[str, Any]:
    """One page of stored series, without chapter lists. Drives the dashboard.

    Served from the in-memory summary index, so paging costs no database reads.
    """
    src = _resolve(source)
    try:
        items = await series_index.get(src.collection)
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")
    return query_feed(items, sort=sort, month=month, q=q, offset=offset, limit=limit)


@router.get("/{source}/series", response_model=List[VortexSeries])
async def list_source_series(
    source: str = SourcePath,
    limit: int = Query(200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Everything stored for one source, chapters included. Heavy: the
    dashboard uses /feed instead."""
    src = _resolve(source)
    try:
        return await run_in_threadpool(firebase_service.list_documents, src.collection, limit)
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")


@router.get("/{source}/series/{slug:path}", response_model=VortexSeries)
async def get_source_series(source: str, slug: str) -> Dict[str, Any]:
    """One stored series. Slugs may contain apostrophes, hence the path converter."""
    src = _resolve(source)
    try:
        doc = await run_in_threadpool(firebase_service.get_document, src.collection, slug)
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")
    if doc is None:
        raise HTTPException(404, f"Series '{slug}' not found in {source}")
    return doc


# Covers never change once uploaded, but each one costs a fetch from the
# source - for Thunder through a Cloudflare session - so without this every
# visit pays again for every card. Bounded by bytes: the box is small.
COVER_CACHE_BYTES = 48 * 1024 * 1024
_covers: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()
_covers_size = 0


def _remember_cover(url: str, content: bytes, content_type: str) -> None:
    global _covers_size
    if len(content) > COVER_CACHE_BYTES // 8 or url in _covers:
        return
    _covers[url] = (content, content_type)
    _covers_size += len(content)
    while _covers_size > COVER_CACHE_BYTES:
        _, (old, _) = _covers.popitem(last=False)
        _covers_size -= len(old)


@router.get("/{source}/cover")
async def source_cover(
    source: str = SourcePath,
    url: str = Query(..., description="Cover URL from a stored series of this source"),
) -> Response:
    """Serve a cover image through the backend.

    Thunder's covers live on a Cloudflare-protected origin that answers a
    hotlinked <img> request with a challenge page, so the browser renders a
    broken image no matter what referrer policy it sends. The backend already
    holds a cleared session for that origin, so it fetches the bytes and hands
    them over same-origin.
    """
    src = _resolve(source)

    # Only ever fetch what belongs to this source: an open proxy would let any
    # caller pull arbitrary URLs through this server. See Source.allows_cover
    # for why a CDN in front of the origin still has to name it.
    parsed = httpx.URL(url)
    if not src.allows_cover(parsed):
        raise HTTPException(
            400, f"URL host {parsed.host!r} does not belong to {src.site}"
        )

    cached = _covers.get(url)
    if cached is not None:
        _covers.move_to_end(url)
        content, content_type = cached
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    try:
        if src.session is not None:
            response = await src.session.get(url)
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(url)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Cover fetch failed for {url}: {e}")
        raise HTTPException(502, f"Could not fetch cover: {e}")

    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(502, f"Upstream returned {content_type or 'no content type'}, not an image")

    _remember_cover(url, response.content, content_type)
    return Response(
        content=response.content,
        media_type=content_type,
        # Covers never change once uploaded, so let the browser and any CDN in
        # front of this keep them for a day.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{source}/status")
async def source_status(source: str = SourcePath) -> Dict[str, Any]:
    """Stored count, scheduler state and the last run for one source."""
    src = _resolve(source)
    try:
        # Counted from the summary index the feed loads anyway, rather than
        # streaming every document id on each page view.
        stored = len(await series_index.get(src.collection))
        db_ok = True
    except Exception as e:
        logger.error(f"Status: database unreachable: {e}")
        stored, db_ok = 0, False

    last = getattr(src.scraper, "last_result", None)
    last_at = getattr(src.scraper, "last_run_at", None)
    return {
        "source": src.id,
        "name": src.name,
        "site": src.site,
        "notes": src.notes,
        "database_connected": db_ok,
        "stored_series": stored,
        "scheduler": get_scheduler_status(),
        "last_run_at": last_at.isoformat() if last_at else None,
        "last_result": last.model_dump(mode="json") if last else None,
    }
