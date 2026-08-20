"""Scraper for en-thunderscans.com.

Site-specific by design, like the vortexscans module. A WordPress site, so
the shapes are completely different and the two scrapers stay separate.

Strategy
--------
1. `wp-sitemap-posts-manga-1.xml` lists every series (900 today). That is the
   discovery source and gives the set of valid slugs.

2. Chapter posts live in `wp-sitemap-posts-post-N.xml`. Their URLs use two
   different shapes — `{slug}-chapter-{n}` and plain `{slug}-{n}` — so they
   are matched back to a series by testing the prefix against known slugs
   rather than by pattern alone.

3. Sitemap `lastmod` is a *modified* time, not a publish time: a long-running
   series that was edited recently looks like it debuted this month. So the
   sitemap only nominates candidates; the debut date is confirmed from the
   publish dates printed on the series page, which are authoritative.

robots.txt here disallows only /wp-admin/, so everything below is permitted.

Since August 2026 the whole origin — pages, sitemaps and cover images alike —
sits behind a Cloudflare JS challenge, so every request goes through the shared
`CloudflareSession` rather than a bare httpx client. See app/services/cloudflare.py.
"""

import asyncio
import html as html_lib
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import httpx
from selectolax.parser import HTMLParser

from app.core.logging import get_logger
from app.models.vortexscans import ChapterInfo, ScrapeResult, VortexSeries
from app.services.cloudflare import ChallengeError, thunderscans_session
from app.services.vortexscans import normalize_title

logger = get_logger(__name__)

BASE_URL = "https://en-thunderscans.com"
SITEMAP_INDEX = f"{BASE_URL}/sitemap.xml"
SERIES_SITEMAP = f"{BASE_URL}/wp-sitemap-posts-manga-1.xml"
COLLECTION = "thunderscans_series"

REQUEST_DELAY = 0.4
SITEMAP_CONCURRENCY = 4
CACHE_TTL = 900

# --- source-specific rules -------------------------------------------------
ALLOWED_TYPES = {"manhwa", "manhua"}

# Genre keywords that disqualify a series. Matching is token-based and
# case/plural insensitive, so "Adults", "adult (18+)" and "ADULT" all hit
# the "adult" rule, and "Gender Bender" matches as a two-word phrase.
BLOCKED_GENRES = (
    "adult",
    "ecchi",
    "game",
    "gender bender",
    "idols",
    "manhwa hot",
    "mature",
    "yuri",
)

_ENTRY_RE = re.compile(r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", re.S)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>")
_SERIES_SLUG_RE = re.compile(r"/comics/([^/]+)/?$")

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        1,
    )
}
_DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),\s*(\d{4})",
    re.I,
)


def _norm_tokens(text: str) -> List[str]:
    """Lowercase, strip punctuation, and singularise each word."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", html_lib.unescape(text or "").lower())
    tokens = []
    for word in cleaned.split():
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.append(word)
    return tokens


_BLOCKED_TOKEN_LISTS = [_norm_tokens(g) for g in BLOCKED_GENRES if g]


def is_blocked_genre(genre: str) -> bool:
    """True when a genre label matches one of the blocked keywords.

    Deliberately not a plain substring test. "Game" must block the genre
    "Game"/"Games", but must not block "Adventure Game Club" — so a keyword
    matches only when it *is* the label, allowing at most one extra word for
    qualifiers like "Adult (18+)". Spacing is ignored too, so "GenderBender"
    still matches "Gender Bender".
    """
    tokens = _norm_tokens(genre)
    if not tokens:
        return False
    token_set = set(tokens)
    squashed = "".join(tokens)

    for blocked in _BLOCKED_TOKEN_LISTS:
        if token_set == set(blocked):
            return True
        if squashed == "".join(blocked):
            return True
        if set(blocked) <= token_set and len(tokens) <= len(blocked) + 1:
            return True
    return False


def blocked_genres_in(genres: List[str]) -> List[str]:
    """Which of a series' genres are disqualifying, if any."""
    return [g for g in genres if is_blocked_genre(g)]


def is_allowed_type(series_type: Optional[str]) -> bool:
    """Only Manhwa and Manhua are wanted from this source."""
    return bool(series_type) and series_type.strip().lower() in ALLOWED_TYPES


def _parse_date(text: str) -> Optional[datetime]:
    """Parse 'August 5, 2026' as printed in the chapter rows."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(
            int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)),
            tzinfo=timezone.utc,
        )
    except (ValueError, KeyError):
        return None


def _chapter_sort_key(number: str) -> float:
    try:
        return float(number)
    except ValueError:
        return -1.0


class ThunderScansScraper:
    """Scrapes new series from en-thunderscans.com."""

    LABELS = ("Type", "Status", "Author", "Artist", "Released", "Updated On", "Posted On")

    def __init__(self) -> None:
        self._slugs: Optional[Set[str]] = None
        self._index: Optional[Dict[str, List[Tuple[str, Optional[datetime]]]]] = None
        self._index_at: float = 0.0
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ScrapeResult] = None

    # ------------------------------------------------------------ discovery

    async def fetch_chapter_index(
        self, force: bool = False
    ) -> Dict[str, List[Tuple[str, Optional[datetime]]]]:
        """slug -> [(chapter_number, lastmod)] built from the WordPress sitemaps."""
        age = time.time() - self._index_at
        if self._index is not None and not force and age < CACHE_TTL:
            logger.info(f"Using cached thunder index ({age:.0f}s old)")
            return self._index

        # The two sitemap fetches below go through the session, so by the time
        # the fan-out starts any clearance the origin demands is already in
        # hand and shared, rather than being raced for by every page at once.
        series_resp = await thunderscans_session.get(SERIES_SITEMAP)
        series_resp.raise_for_status()
        slugs: Set[str] = set()
        for loc in _LOC_RE.findall(series_resp.text):
            m = _SERIES_SLUG_RE.search(loc)
            if m:
                slugs.add(html_lib.unescape(m.group(1)))
        if not slugs:
            # A challenge page parses as "zero series" without erroring. Left
            # unchecked that reads as a clean run that simply found nothing new,
            # which is how a month of missing series went unnoticed before.
            raise ChallengeError(
                f"Series sitemap returned no slugs ({len(series_resp.text)} bytes). "
                "The Cloudflare clearance is probably not working."
            )
        self._slugs = slugs
        logger.info(f"Thunder series sitemap: {len(slugs)} series")

        index_resp = await thunderscans_session.get(SITEMAP_INDEX)
        index_resp.raise_for_status()
        maps = [u for u in _LOC_RE.findall(index_resp.text) if "posts-post-" in u]

        semaphore = asyncio.Semaphore(SITEMAP_CONCURRENCY)

        async with thunderscans_session.client() as client:

            async def fetch_one(url: str) -> str:
                async with semaphore:
                    await asyncio.sleep(REQUEST_DELAY)
                    r = await client.get(url)
                    r.raise_for_status()
                    return r.text

            pages = await asyncio.gather(*(fetch_one(u) for u in maps))

        index: Dict[str, List[Tuple[str, Optional[datetime]]]] = {}
        matched = 0
        for page in pages:
            for loc, mod in _ENTRY_RE.findall(page):
                hit = self._match_chapter(loc, slugs)
                if not hit:
                    continue
                slug, number = hit
                try:
                    ts = datetime.fromisoformat(mod.strip())
                except ValueError:
                    ts = None
                index.setdefault(slug, []).append((number, ts))
                matched += 1

        for slug in index:
            index[slug].sort(key=lambda pair: _chapter_sort_key(pair[0]))

        self._index = index
        self._index_at = time.time()
        logger.info(f"Thunder chapter index: {matched} chapters across {len(index)} series")
        return index

    @staticmethod
    def _match_chapter(loc: str, slugs: Set[str]) -> Optional[Tuple[str, str]]:
        """Map a chapter URL back to (series_slug, chapter_number).

        Two URL shapes are in use and a slug can itself end in digits, so the
        prefix is validated against the known slug set rather than guessed.
        """
        path = re.sub(r"^https?://[^/]+/", "", loc).strip("/")
        for pattern in (r"^(.*)-chapter-([\d.]+)$", r"^(.*)-([\d.]+)$"):
            m = re.match(pattern, path)
            if m and m.group(1) in slugs:
                return m.group(1), m.group(2)
        return None

    # ---------------------------------------------------------- series page

    async def fetch_series_page(self, slug: str) -> Optional[dict]:
        """Scrape one series page: metadata, genres, and dated chapter rows."""
        url = f"{BASE_URL}/comics/{slug}/"
        try:
            resp = await thunderscans_session.get(url)
            resp.raise_for_status()
        except (httpx.HTTPError, ChallengeError) as e:
            logger.error(f"Thunder series page failed for {slug}: {e}")
            return None

        tree = HTMLParser(resp.text)

        name = None
        for node in tree.css("h1"):
            text = node.text(strip=True)
            if text and text not in self.LABELS:
                name = text
                break

        genres = []
        for a in tree.css('a[href*="/genres/"]'):
            text = a.text(strip=True)
            if text and text not in genres:
                genres.append(text)

        rating = None
        for node in tree.css("[class*=rating], [class*=score]"):
            try:
                rating = float(node.text(strip=True))
                break
            except ValueError:
                continue

        cover = None
        for img in tree.css("img"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if "/wp-content/uploads/" in src:
                cover = src
                break

        # Chapter rows carry the real publish dates, e.g. "Chapter 1April 5, 2026".
        chapters: Dict[str, Optional[datetime]] = {}
        for a in tree.css("a[href]"):
            hit = self._match_chapter(a.attributes.get("href", ""), self._slugs or {slug})
            if not hit or hit[0] != slug:
                continue
            number = hit[1]
            released = _parse_date(a.text(strip=True))
            if number not in chapters or (released and chapters[number] is None):
                chapters[number] = released

        return {
            "slug": slug,
            "name": html_lib.unescape(name) if name else slug.replace("-", " ").title(),
            "url": url,
            "cover_url": cover,
            "rating": rating,
            "type": self._labelled_value(tree, "Type"),
            "status": self._labelled_value(tree, "Status"),
            "genres": genres,
            "page_chapters": sorted(chapters.items(), key=lambda p: _chapter_sort_key(p[0])),
        }

    def _labelled_value(self, tree: HTMLParser, label: str) -> Optional[str]:
        for node in tree.css("h1, h2, h3, span, div, td, b, strong"):
            if node.text(strip=True) != label or node.parent is None:
                continue
            parts = [p.strip() for p in node.parent.text(separator="|", strip=True).split("|")]
            parts = [p for p in parts if p and p != label]
            if parts:
                return parts[0]
        return None

    # ------------------------------------------------------------- assembly

    def build_series(self, page: dict) -> VortexSeries:
        chapters = [
            ChapterInfo(
                number=number,
                url=f"{BASE_URL}/{page['slug']}-chapter-{number}/",
                released_at=released,
            )
            for number, released in page["page_chapters"]
        ]
        dated = [c for c in chapters if c.released_at]
        newest = max(chapters, key=lambda c: _chapter_sort_key(c.number)) if chapters else None

        return VortexSeries(
            slug=page["slug"],
            name=page["name"],
            url=page["url"],
            cover_url=page.get("cover_url"),
            rating=page.get("rating"),
            type=page.get("type"),
            status=page.get("status"),
            genres=page.get("genres", []),
            total_chapters=len(chapters),
            # Same rule the month filter uses, so the stored date always agrees
            # with the month a series was captured under. A prologue numbered 0
            # can predate chapter 1 by weeks and must not become the debut.
            first_chapter_released_at=self._debut_from(page["page_chapters"])
            or min((c.released_at for c in dated), default=None),
            last_updated_at=max((c.released_at for c in dated), default=None),
            latest_chapter=newest.number if newest else None,
            latest_chapter_url=newest.url if newest else None,
            chapters=chapters,
            source="en-thunderscans.com",
            scraped_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _debut_from(chapters: List[Tuple[str, Optional[datetime]]]) -> Optional[datetime]:
        dated = [(n, t) for n, t in chapters if t]
        if not dated:
            return None
        # Compared numerically, so "01" and "1.0" count as chapter 1 too.
        exact = next((t for n, t in dated if _chapter_sort_key(n) == 1.0), None)
        if exact:
            return exact
        number, ts = min(dated, key=lambda pair: _chapter_sort_key(pair[0]))
        return ts if _chapter_sort_key(number) <= 2 else None

    # -------------------------------------------------------- orchestration

    async def scrape_new_series(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        existing_slugs: Optional[set] = None,
        existing_titles: Optional[set] = None,
    ) -> Tuple[ScrapeResult, List[VortexSeries]]:
        started = time.time()
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month
        existing_slugs = existing_slugs or set()

        index = await self.fetch_chapter_index()
        result = ScrapeResult(month=f"{year:04d}-{month:02d}", candidates_seen=len(index))
        series_list: List[VortexSeries] = []

        # 1. Nominate candidates from sitemap timestamps. These are modified
        #    times, so this over-selects on purpose; step 3 confirms.
        candidates = []
        for slug, chapters in index.items():
            ts = self._debut_from(chapters)
            if ts and ts.year == year and ts.month == month:
                candidates.append(slug)
        logger.info(f"Thunder candidates from sitemap: {len(candidates)}")

        # 2. Skip what is already stored before fetching anything.
        todo = []
        for slug in sorted(candidates):
            if slug in existing_slugs:
                result.already_stored += 1
                result.skipped_slugs.append(slug)
            else:
                todo.append(slug)

        # 3. Confirm against the series page and apply this source's filters.
        seen_titles = set(existing_titles or set())
        confirmed = 0
        for slug in todo:
            await asyncio.sleep(REQUEST_DELAY)
            page = await self.fetch_series_page(slug)
            if page is None:
                continue

            debut = self._debut_from(page["page_chapters"])
            if not (debut and debut.year == year and debut.month == month):
                continue
            confirmed += 1

            if not is_allowed_type(page.get("type")):
                logger.info(f"Skipping {slug}: type is {page.get('type')!r}")
                continue

            blocked = blocked_genres_in(page.get("genres", []))
            if blocked:
                logger.info(f"Skipping {slug}: blocked genre(s) {blocked}")
                continue

            key = normalize_title(page["name"])
            if key in seen_titles:
                logger.info(f"Skipping duplicate title: {page['name']} ({slug})")
                result.already_stored += 1
                result.skipped_slugs.append(slug)
                continue
            seen_titles.add(key)

            series_list.append(self.build_series(page))

        result.matched_month = confirmed + result.already_stored
        result.duration_seconds = round(time.time() - started, 2)
        logger.info(
            f"Thunder scrape: {len(candidates)} candidates, {confirmed} confirmed debuts, "
            f"{len(series_list)} pass filters, {result.already_stored} already stored"
        )
        return result, series_list

    async def run_and_store(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        dry_run: bool = False,
    ) -> ScrapeResult:
        from starlette.concurrency import run_in_threadpool

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
                await run_in_threadpool(
                    firebase_service.set_document,
                    COLLECTION,
                    series.slug,
                    series.model_dump(mode="json"),
                )
                result.saved += 1
                result.saved_slugs.append(series.slug)
                logger.info(f"Saved thunder series: {series.slug}")
            except Exception as e:
                logger.error(f"Failed to save {series.slug}: {e}")

        self.last_result = result
        self.last_run_at = datetime.now(timezone.utc)
        return result


thunderscans_scraper = ThunderScansScraper()
