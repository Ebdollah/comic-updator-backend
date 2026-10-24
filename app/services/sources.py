"""Registry of scrapeable sources.

Each scraper is written against one site and they share no parsing code —
the sites have nothing structurally in common. What they do share is this
interface, which is all the API and scheduler need:

    run_and_store(year, month, dry_run) -> ScrapeResult
    scrape_new_series(...)              -> (ScrapeResult, [VortexSeries])
    last_run_at, last_result

To add a source: write its scraper module, then add one entry here.

Callers that want a regular current-month run should go through run_source
or scrape_all rather than the scraper directly: those share a run already in
progress and refresh the feed's summary index when it finishes.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from app.core.logging import get_logger
from app.models.vortexscans import MultiScrapeResult, ScrapeResult, SourceScrapeOutcome
from app.services import thunderscans, vortexscans
from app.services.cloudflare import CloudflareSession, thunderscans_session
from app.services.series_index import series_index

logger = get_logger(__name__)

# Jetpack's image CDN. Vortex started routing covers through it, so a stored
# cover_url can name one of these hosts rather than the site's own. A Photon
# URL carries the origin it is proxying as its first path segment:
# https://i0.wp.com/storage.vortexscans.org/upload/series/... .
PHOTON_HOSTS = ("i0.wp.com", "i1.wp.com", "i2.wp.com", "i3.wp.com")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    site: str
    collection: str
    scraper: Any
    notes: str = ""
    # Set when the origin is behind a Cloudflare challenge, so the cover proxy
    # knows to fetch images through a cleared session rather than plainly.
    session: Optional[CloudflareSession] = None

    def owns_host(self, host: str) -> bool:
        """True for this source's own domain or any subdomain of it."""
        return bool(host) and (host == self.site or host.endswith(f".{self.site}"))

    def allows_cover(self, url: httpx.URL) -> bool:
        """Whether the cover proxy may fetch this URL on the source's behalf.

        The proxy must never become an open relay, so this is a allowlist, not
        a sanity check. Photon hosts are accepted only when the origin encoded
        in the path is one of ours: anyone can ask Jetpack to proxy anything,
        so trusting i0.wp.com wholesale would reopen exactly the hole the host
        check exists to close.
        """
        host = url.host or ""
        if self.owns_host(host):
            return True
        if host in PHOTON_HOSTS:
            return self.owns_host(url.path.lstrip("/").split("/")[0])
        return False


SOURCES: Dict[str, Source] = {
    "vortexscans": Source(
        id="vortexscans",
        name="Vortex Scans",
        site="vortexscans.org",
        collection=vortexscans.COLLECTION,
        scraper=vortexscans.vortexscans_scraper,
        notes="Series whose chapter 1 released in the target month.",
    ),
    "thunderscans": Source(
        id="thunderscans",
        name="Thunder Scans",
        site="en-thunderscans.com",
        collection=thunderscans.COLLECTION,
        scraper=thunderscans.thunderscans_scraper,
        notes=(
            "Same debut-month rule, plus: type must be Manhwa or Manhua, and "
            "series tagged adult, ecchi, game, gender bender, idols, manhwa hot, "
            "mature or yuri are excluded."
        ),
        session=thunderscans_session,
    ),
}

DEFAULT_SOURCE = "vortexscans"


def get_source(source_id: str) -> Source:
    """Look up a source, raising KeyError if the id is unknown."""
    return SOURCES[source_id]


def list_sources() -> List[dict]:
    return [
        {
            "id": s.id,
            "name": s.name,
            "site": s.site,
            "collection": s.collection,
            "notes": s.notes,
        }
        for s in SOURCES.values()
    ]


_inflight: Dict[str, "asyncio.Task[ScrapeResult]"] = {}


async def _run_and_invalidate(source: Source) -> ScrapeResult:
    try:
        return await source.scraper.run_and_store()
    finally:
        series_index.invalidate(source.collection)


async def run_source(source: Source) -> ScrapeResult:
    """Scrape and store one source for the current month.

    The scheduler, the per-source endpoint and scrape-all can all ask for the
    same source at once. A second crawl of the same site would only find what
    the first is about to save, so later callers join the run in progress.
    The shield keeps a caller that disconnects from cancelling it for the rest.
    """
    task = _inflight.get(source.id)
    if task is None:
        task = asyncio.create_task(_run_and_invalidate(source))
        _inflight[source.id] = task
        task.add_done_callback(lambda _t, sid=source.id: _inflight.pop(sid, None))
    return await asyncio.shield(task)


async def scrape_all() -> MultiScrapeResult:
    """Scrape every source concurrently; one failing never sinks the others."""
    started = time.monotonic()

    async def one(source: Source) -> SourceScrapeOutcome:
        try:
            result = await run_source(source)
            return SourceScrapeOutcome(source=source.id, name=source.name, ok=True, result=result)
        except Exception as e:
            logger.error(f"Scrape-all: {source.id} failed: {e}")
            return SourceScrapeOutcome(
                source=source.id, name=source.name, ok=False, error=str(e) or type(e).__name__
            )

    outcomes = await asyncio.gather(*(one(s) for s in SOURCES.values()))
    return MultiScrapeResult(
        total_saved=sum(o.result.saved for o in outcomes if o.result),
        duration_seconds=round(time.monotonic() - started, 1),
        sources=list(outcomes),
    )
