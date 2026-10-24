from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class ChapterInfo(BaseModel):
    """A single chapter and when it was released."""

    number: str = Field(..., description="Chapter number as shown on the site, e.g. '45' or '12.5'")
    url: str
    released_at: Optional[datetime] = Field(None, description="UTC release timestamp")


class SeriesSummary(BaseModel):
    """Everything about a series except its chapter list - what the feed shows."""

    slug: str = Field(..., description="URL slug, also used as the Firestore document id")
    name: str
    url: str
    cover_url: Optional[str] = None
    rating: Optional[float] = None
    type: Optional[str] = Field(None, description="Manhwa / Manga / Manhua")
    status: Optional[str] = Field(None, description="Ongoing / Completed / Hiatus")
    genres: List[str] = Field(default_factory=list, description="Genre tags, when the source lists them")

    total_chapters: int = 0
    first_chapter_released_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = Field(None, description="Release time of the newest chapter")
    latest_chapter: Optional[str] = None
    latest_chapter_url: Optional[str] = None

    source: str = "vortexscans.org"
    scraped_at: Optional[datetime] = None


class VortexSeries(SeriesSummary):
    """A stored series in full, as scraped from its source."""

    chapters: List[ChapterInfo] = Field(default_factory=list)


class MonthCount(BaseModel):
    month: str = Field(..., description="Debut month, e.g. '2026-08'")
    count: int


class FeedPage(BaseModel):
    """One page of the dashboard feed, plus the counts its filters need."""

    items: List[SeriesSummary]
    total: int = Field(..., description="Series matching the filters, across all pages")
    offset: int
    next_offset: Optional[int] = Field(None, description="Pass back to get the next page; null at the end")
    all_count: int = Field(..., description="Every stored series for the source, unfiltered")
    months: List[MonthCount] = Field(default_factory=list, description="Debut months, newest first")


class ScrapeResult(BaseModel):
    """Summary of one scrape run."""

    month: str = Field(..., description="The month that was filtered on, e.g. '2026-08'")
    candidates_seen: int = Field(0, description="Series found in Latest Releases")
    matched_month: int = Field(0, description="Of those, chapter 1 released in the target month")
    already_stored: int = Field(0, description="Skipped because they were already in the database")
    saved: int = 0
    saved_slugs: List[str] = Field(default_factory=list)
    skipped_slugs: List[str] = Field(default_factory=list)
    dry_run: bool = False
    duration_seconds: float = 0.0


class SourceScrapeOutcome(BaseModel):
    """How one source fared in a scrape-all run."""

    source: str
    name: str
    ok: bool
    result: Optional[ScrapeResult] = None
    error: Optional[str] = None


class MultiScrapeResult(BaseModel):
    """Summary of scraping every source at once."""

    total_saved: int = 0
    duration_seconds: float = 0.0
    sources: List[SourceScrapeOutcome] = Field(default_factory=list)
