from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class ChapterInfo(BaseModel):
    """A single chapter and when it was released."""

    number: str = Field(..., description="Chapter number as shown on the site, e.g. '45' or '12.5'")
    url: str
    released_at: Optional[datetime] = Field(None, description="UTC release timestamp")


class VortexSeries(BaseModel):
    """A series scraped from vortexscans.org."""

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

    chapters: List[ChapterInfo] = Field(default_factory=list)

    source: str = "vortexscans.org"
    scraped_at: Optional[datetime] = None


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
