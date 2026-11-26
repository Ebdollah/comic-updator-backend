"""Scraper for kuaikanmanhua.com (快看漫画) - Chinese manhua only.

Site-specific like the other scraper modules, and deliberately independent of
them: the debut rule is the same, but the filters below apply to this source
alone.

Strategy
--------
1. Discovery comes from the site's own filter API, the one behind the
   "newly added" sort on /tag/0. With the Chinese area selected it returns
   every series that debuted in roughly the last three months, newest first,
   and each entry carries `first_comic_publish_time` - the date chapter 1
   went out - so the month filter needs no further requests.

   robots.txt disallows every URL with a query string (`Disallow: /*?*`),
   which covers this API, and the sitemap stopped being updated in July 2026
   and never listed series. The owner chose to use the API anyway, so it is
   kept to a handful of requests per run with a pause between each.

2. For each new debut that survives the theme filter, the series page
   (/web/topic/{id}/ - allowed by robots.txt) supplies status, the full tag
   list and the chapter list. Its data sits in the page's minified Nuxt
   state, read by app.utils.nuxt without executing anything. The JSON API
   that page's own scripts call answers plain requests with a 403, so it is
   not used.

Text is stored as the site prints it, in Chinese. Covers go to the B2 bucket
like the other sources.
"""

import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import httpx

from app.core.logging import get_logger
from app.models.vortexscans import ChapterInfo, ScrapeResult, VortexSeries
from app.utils.nuxt import NuxtParseError, parse_nuxt_state

logger = get_logger(__name__)

BASE_URL = "https://www.kuaikanmanhua.com"
FILTER_API = f"{BASE_URL}/search/mini/topic/multi_filter"
COLLECTION = "kuaikan_series"
SITE = "kuaikanmanhua.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

REQUEST_DELAY = 1.5
PAGE_SIZE = 48
MAX_PAGES = 5  # the newly-added list is ~30 series today; this is a safety cap

# Filter API values, as the site's own filter bar sends them.
REGION_CHINESE = 2  # area: 1 all, 2 Chinese (国漫), 3 Korean, 4 Japanese
SORT_NEWLY_ADDED = 3  # sort: 1 recommended, 2 hottest, 3 newly added

# The site is on Beijing time; its dates are calendar dates there.
SITE_TZ = timezone(timedelta(hours=8))

# --- source-specific rules -------------------------------------------------
# A series is dropped when any of its tags is one of these. Keys are the
# site's tags, values what the filter bar shows them as in English.
EXCLUDED_THEMES: Dict[str, str] = {
    "恋爱": "in love (romance)",
    "校园": "campus (school)",
}

STATUS_LABELS = {"连载中": "Ongoing", "连载": "Ongoing", "已完结": "Completed", "完结": "Completed"}

_CHAPTER_NUMBER_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话話]")
_POSTER_SIZE_RE = re.compile(r"(\.(?:webp|jpe?g|png))-w\d+[^/]*$", re.I)
_MONTH_DAY_RE = re.compile(r"^(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2})$")


def normalize_title(title: str) -> str:
    """Comparison key for a title in any script.

    The Vortex helper keeps only a-z and 0-9, which folds every Chinese title
    to the same empty string. This keeps letters and digits of any script.
    """
    text = unicodedata.normalize("NFKC", title or "").casefold()
    return "".join(ch for ch in text if ch.isalnum())


def excluded_themes_in(tags: List[str]) -> List[str]:
    return [t for t in tags if t in EXCLUDED_THEMES]


def parse_debut(value: str) -> Optional[datetime]:
    """'2026.11.2' as printed by the filter API.

    Stored as midnight UTC of that calendar date rather than converted from
    Beijing time: conversion would move a debut on the 1st into the previous
    month, out of step with the month the site (and this filter) put it in.
    """
    try:
        y, m, d = (int(part) for part in (value or "").split("."))
        return datetime(y, m, d, tzinfo=timezone.utc)
    except ValueError:
        return None


def poster_url(url: Optional[str]) -> Optional[str]:
    """The 750px-wide portrait poster instead of the 320px list thumbnail."""
    if not url:
        return url
    return _POSTER_SIZE_RE.sub(r"\1-w750", url)


def chapter_number(title: str) -> Optional[str]:
    """'12' from '第12话：…'; None for previews and extras with no number."""
    m = _CHAPTER_NUMBER_RE.search(title or "")
    return m.group(1) if m else None


def chapter_dates(raw: List[str], debut: Optional[datetime], today: datetime) -> List[Optional[datetime]]:
    """Dates for the chapter list, whose entries read '11-02' with no year.

    Chapters are listed oldest first, so the year starts at the debut's and
    moves on whenever the month goes backwards. '今天'/'昨天' are today and
    yesterday in Beijing time.
    """
    year = (debut or today).year
    last_month = 0
    out: List[Optional[datetime]] = []
    for text in raw:
        text = (text or "").strip()
        if text in ("今天", "昨天"):
            day = today - timedelta(days=1 if text == "昨天" else 0)
            out.append(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
            continue
        m = _MONTH_DAY_RE.match(text)
        if not m:
            out.append(None)
            continue
        if m.group(1):
            year = int(m.group(1))
        month, day = int(m.group(2)), int(m.group(3))
        if month < last_month and not m.group(1):
            year += 1
        last_month = month
        try:
            out.append(datetime(year, month, day, tzinfo=timezone.utc))
        except ValueError:
            out.append(None)
    return out


class KuaikanScraper:
    """Scrapes new Chinese series from kuaikanmanhua.com."""

    def __init__(self) -> None:
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ScrapeResult] = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{BASE_URL}/tag/0",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )

    # ------------------------------------------------------------ discovery

    async def fetch_newly_added(self, client: httpx.AsyncClient) -> List[dict]:
        """Every entry on the Chinese "newly added" list, newest debut first."""
        entries: List[dict] = []
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                await asyncio.sleep(REQUEST_DELAY)
            response = await client.get(
                FILTER_API,
                params={
                    "page": page,
                    "size": PAGE_SIZE,
                    "tag_id": 0,
                    "update_status": 0,
                    "pay_status": 0,
                    "label_dimension_origin": REGION_CHINESE,
                    "sort": SORT_NEWLY_ADDED,
                },
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 200:
                raise RuntimeError(f"Kuaikan filter API said {body.get('code')}: {body.get('message')}")
            batch = (body.get("hits") or {}).get("topicMessageList") or []
            entries.extend(batch)
            if len(batch) < PAGE_SIZE or len(entries) >= int(body.get("total") or 0):
                break
        return entries

    async def fetch_series_page(self, client: httpx.AsyncClient, topic_id: int) -> Optional[dict]:
        """Status, tags and chapters from the series page, or None if unreadable."""
        url = f"{BASE_URL}/web/topic/{topic_id}/"
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = parse_nuxt_state(response.text)["data"][0]
            info = data.get("topicInfo") or {}
            comics = data.get("comics") or info.get("comics") or []
            if not info:
                # Some series the filter API lists are not viewable on the web
                # (app-only, it seems): the page renders with curCode 10104
                # and no series data. The list entry still has the essentials.
                raise NuxtParseError(f"page has no series data (site code {data.get('curCode')})")
            return {"info": info, "comics": comics}
        except (httpx.HTTPError, NuxtParseError, KeyError, IndexError, TypeError) as e:
            logger.warning(f"Kuaikan series page {topic_id} unreadable, using list data only: {e}")
            return None

    # ------------------------------------------------------------- assembly

    def build_series(self, entry: dict, page: Optional[dict]) -> VortexSeries:
        topic_id = str(entry["topic_id"])
        debut = parse_debut(entry.get("first_comic_publish_time", ""))
        info = (page or {}).get("info") or {}
        comics = (page or {}).get("comics") or []

        today = datetime.now(SITE_TZ)
        dates = chapter_dates([c.get("created_at") for c in comics], debut, today)
        chapters = []
        for comic, released in zip(comics, dates):
            number = chapter_number(comic.get("title", ""))
            if number is None:
                continue  # 预告 (preview) and other unnumbered extras
            chapters.append(
                ChapterInfo(
                    number=number,
                    url=f"{BASE_URL}/web/comic/{comic['id']}/",
                    released_at=released,
                )
            )

        newest = max(chapters, key=lambda c: float(c.number)) if chapters else None
        dated = [c.released_at for c in chapters if c.released_at]
        status = info.get("update_status")

        return VortexSeries(
            slug=topic_id,
            name=info.get("title") or entry["title"],
            url=f"{BASE_URL}/web/topic/{topic_id}/",
            cover_url=poster_url(info.get("vertical_image_url") or entry.get("vertical_image_url")),
            rating=None,
            type="Manhua",
            status=STATUS_LABELS.get(status, status),
            genres=_merge_tags(entry.get("category"), info.get("tags")),
            total_chapters=len(chapters) if comics else int(entry.get("comics_count") or 0),
            first_chapter_released_at=debut,
            last_updated_at=max(dated, default=None),
            latest_chapter=newest.number if newest else chapter_number(entry.get("latest_comic_title", "")),
            latest_chapter_url=newest.url if newest else None,
            chapters=chapters,
            source=SITE,
            scraped_at=datetime.now(timezone.utc),
        )

    # -------------------------------------------------------- orchestration

    async def scrape_new_series(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        existing_slugs: Optional[set] = None,
        existing_titles: Optional[set] = None,
    ) -> Tuple[ScrapeResult, List[VortexSeries]]:
        started = time.time()
        now = datetime.now(SITE_TZ)
        year = year or now.year
        month = month or now.month
        existing_slugs = existing_slugs or set()
        seen_titles: Set[str] = set(existing_titles or set())

        async with self._client() as client:
            entries = await self.fetch_newly_added(client)
            result = ScrapeResult(month=f"{year:04d}-{month:02d}", candidates_seen=len(entries))
            series_list: List[VortexSeries] = []

            for entry in entries:
                debut = parse_debut(entry.get("first_comic_publish_time", ""))
                if not (debut and debut.year == year and debut.month == month):
                    continue
                result.matched_month += 1
                slug = str(entry["topic_id"])

                if slug in existing_slugs:
                    result.already_stored += 1
                    result.skipped_slugs.append(slug)
                    continue

                # The list's tags are a subset of the page's; checking them
                # first saves a page fetch for most excluded series.
                blocked = excluded_themes_in(entry.get("category") or [])
                if blocked:
                    logger.info(f"Skipping kuaikan {slug} {entry.get('title')}: excluded theme(s) {blocked}")
                    continue

                await asyncio.sleep(REQUEST_DELAY)
                page = await self.fetch_series_page(client, entry["topic_id"])
                blocked = excluded_themes_in((page or {}).get("info", {}).get("tags") or [])
                if blocked:
                    logger.info(f"Skipping kuaikan {slug} {entry.get('title')}: excluded theme(s) {blocked}")
                    continue

                series = self.build_series(entry, page)
                key = normalize_title(series.name)
                if key and key in seen_titles:
                    logger.info(f"Skipping duplicate kuaikan title: {series.name} ({slug})")
                    result.already_stored += 1
                    result.skipped_slugs.append(slug)
                    continue
                seen_titles.add(key)
                series_list.append(series)

        result.duration_seconds = round(time.time() - started, 2)
        logger.info(
            f"Kuaikan scrape: {len(entries)} newly added, {result.matched_month} debuted "
            f"{result.month}, {len(series_list)} pass filters, {result.already_stored} already stored"
        )
        return result, series_list

    async def run_and_store(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        dry_run: bool = False,
    ) -> ScrapeResult:
        from starlette.concurrency import run_in_threadpool

        from app.services.b2_storage import b2_storage
        from app.services.firebase import firebase_service

        existing = await run_in_threadpool(firebase_service.existing_ids, COLLECTION)
        names = await run_in_threadpool(firebase_service.existing_values, COLLECTION, "name")
        result, new_series = await self.scrape_new_series(
            year=year,
            month=month,
            existing_slugs=existing,
            existing_titles={normalize_title(n) for n in names},
        )
        result.dry_run = dry_run

        if dry_run:
            result.saved_slugs = [s.slug for s in new_series]
            return result

        for series in new_series:
            try:
                if series.cover_url and b2_storage.is_configured:
                    b2_url = await b2_storage.download_and_store_image(
                        image_url=series.cover_url,
                        source_id="kuaikan",
                        slug=series.slug,
                    )
                    if b2_url and b2_url != series.cover_url:
                        series.original_cover_url = series.cover_url
                        series.cover_url = b2_url

                await run_in_threadpool(
                    firebase_service.set_document,
                    COLLECTION,
                    series.slug,
                    series.model_dump(mode="json"),
                )
                result.saved += 1
                result.saved_slugs.append(series.slug)
                logger.info(f"Saved kuaikan series: {series.slug} {series.name}")
            except Exception as e:
                logger.error(f"Failed to save kuaikan {series.slug}: {e}")

        self.last_result = result
        self.last_run_at = datetime.now(timezone.utc)
        return result


def _merge_tags(*lists: Optional[List[str]]) -> List[str]:
    """Union of tag lists in first-seen order."""
    seen: Dict[str, None] = {}
    for tags in lists:
        for tag in tags or []:
            if tag:
                seen.setdefault(tag, None)
    return list(seen)


kuaikan_scraper = KuaikanScraper()
