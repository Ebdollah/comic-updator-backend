"""Registry of scrapeable sources.

Each scraper is written against one site and they share no parsing code —
the sites have nothing structurally in common. What they do share is this
interface, which is all the API and scheduler need:

    run_and_store(year, month, dry_run) -> ScrapeResult
    scrape_new_series(...)              -> (ScrapeResult, [VortexSeries])
    last_run_at, last_result

To add a source: write its scraper module, then add one entry here.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services import thunderscans, vortexscans
from app.services.cloudflare import CloudflareSession, thunderscans_session


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
