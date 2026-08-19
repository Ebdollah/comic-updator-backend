"""Scraper for vortexscans.org.

Site-specific by design: selectors and URL shapes here match that site only.
If you add another source later, give it its own module rather than
generalising this one.

Strategy
--------
1. Discovery comes from the site's chapter sitemaps, which list every chapter
   URL with an exact timestamp. That is what makes the "chapter 1 released
   this month" filter possible, and it covers the whole catalogue.

   The homepage cannot be used for discovery: its server-rendered HTML holds
   only the "Hot" tab. The "New" tab is filled in client-side from /api/*,
   which robots.txt disallows, so this module never touches it.

2. Each matching series is then enriched from its own page, which carries the
   name, cover, type, status and chapter count as plain HTML.

3. Rating is only rendered server-side on the homepage cards, so it is taken
   from there when the series happens to appear; otherwise it stays None.

robots.txt allows /series/ and disallows /api/*. This module stays within that.
"""

import asyncio
import html as html_lib
import re
import time
from urllib.parse import quote, unquote
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from selectolax.parser import HTMLParser

from app.core.logging import get_logger
from app.models.vortexscans import ChapterInfo, ScrapeResult, VortexSeries

logger = get_logger(__name__)

BASE_URL = "https://vortexscans.org"
SITEMAP_INDEX = f"{BASE_URL}/sitemap.xml"
BROWSE_URL = f"{BASE_URL}/series/"
COLLECTION = "vortexscans_series"

MAX_BROWSE_PAGES = 40        # /series/ shows 36 per page; ~12 pages today

USER_AGENT = "ComicUpdator/0.1 (personal release tracker; +https://vortexscans.org)"
REQUEST_DELAY = 0.5          # seconds between requests, to stay polite
SITEMAP_CONCURRENCY = 3
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0          # seconds, doubled per retry
SITEMAP_CACHE_TTL = 900      # re-parse the chapter index at most every 15 min
CATALOG_CACHE_TTL = 900      # re-crawl /series/ at most every 15 min

KNOWN_TYPES = {"manhwa", "manga", "manhua", "comic", "novel"}
KNOWN_STATUSES = {"ongoing", "completed", "hiatus", "dropped", "cancelled"}

_CHAPTER_URL_RE = re.compile(r"/series/([^/]+)/chapter-([0-9.]+)")


def slug_from_href(href: str) -> str:
    """Normalise a slug from any source.

    Page links percent-encode characters like ':' while the sitemap does not,
    so 'blade-of-purity%3A-...' and 'blade-of-purity:-...' are the same series.
    Decoding both to one form is what keeps the two sources joined.
    """
    return html_lib.unescape(unquote(href.rstrip("/").split("/")[-1]))


def series_url(slug: str) -> str:
    """Build a request-safe series URL from a decoded slug."""
    return f"{BASE_URL}/series/{quote(slug, safe='')}"


def chapter_url(slug: str, number: str) -> str:
    return f"{series_url(slug)}/chapter-{number}"
_LOC_MOD_RE = re.compile(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", re.S)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>")
_FOUND_COUNT_RE = re.compile(r"Found\s+([\d,]+)\s+series")


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse the site's ISO-8601 timestamps (they end in 'Z')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_title(title: str) -> str:
    """Fold a title down to a comparison key.

    Guards against storing the same comic twice under two slugs: curly vs
    straight apostrophes, punctuation and spacing differences all collapse.
    """
    text = html_lib.unescape(title or "").lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _chapter_sort_key(number: str) -> float:
    try:
        return float(number)
    except ValueError:
        return -1.0


class VortexScansScraper:
    """Scrapes new series from vortexscans.org."""

    def __init__(self) -> None:
        self._chapter_index: Optional[Dict[str, List[Tuple[str, Optional[datetime]]]]] = None
        self._chapter_index_at: float = 0.0
        self._catalog: Optional[Dict[str, dict]] = None
        self._catalog_at: float = 0.0
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ScrapeResult] = None

    # ---------------------------------------------------------------- http

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
            timeout=httpx.Timeout(60.0),
        )

    async def _get(
        self, client: httpx.AsyncClient, url: str, attempts: int = MAX_RETRIES
    ) -> httpx.Response:
        """GET with backoff.

        A single slow response should not abandon a run that has already made
        dozens of requests, so transient timeouts are retried before giving up.
        """
        delay = RETRY_BACKOFF
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as e:
                if attempt == attempts:
                    raise
                logger.warning(
                    f"{type(e).__name__} on {url} (attempt {attempt}/{attempts}), "
                    f"retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    # ------------------------------------------------------- homepage cards

    async def fetch_latest_releases(self) -> Dict[str, dict]:
        """Parse the homepage 'Latest Releases' cards.

        Returns a dict of slug -> presentation metadata. Covers both the Hot
        and New tabs, since both are present in the server-rendered HTML.
        """
        async with self._client() as client:
            resp = await self._get(client, BASE_URL)

        tree = HTMLParser(resp.text)
        section = self._find_latest_releases_section(tree)
        if section is None:
            logger.warning("Latest Releases section not found; site layout may have changed")
            return {}

        cards: Dict[str, dict] = {}
        for anchor in section.css('a[href^="/series/"]'):
            href = anchor.attributes.get("href", "")
            if "/chapter-" in href:
                continue
            slug = slug_from_href(href)
            if not slug or slug in cards:
                continue

            card = self._climb_to_card(anchor)
            if card is None:
                continue

            parsed = self._parse_card(card, slug, href)
            if parsed:
                cards[slug] = parsed

        logger.info(f"Parsed {len(cards)} series cards from Latest Releases")
        return cards

    def _find_latest_releases_section(self, tree: HTMLParser):
        heading = None
        for node in tree.css("h1, h2, h3"):
            if node.text(strip=True) == "Latest Releases":
                heading = node
                break
        if heading is None:
            return None

        node = heading
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            if len(node.css('a[href^="/series/"]')) > 20:
                return node
        return None

    def _climb_to_card(self, anchor):
        """Walk up from a series link to the card element that wraps it.

        Stops as soon as an ancestor covers more than one series, otherwise
        neighbouring cards get merged and their chapter lists mixed together.
        """
        node = anchor
        best = None
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            slugs = {
                a.attributes.get("href", "").rstrip("/").split("/")[-1]
                for a in node.css('a[href^="/series/"]')
                if "/chapter-" not in a.attributes.get("href", "")
            }
            if len(slugs) > 1:
                break
            if node.css("img") and node.css('a[href*="/chapter-"]'):
                best = node
        return best

    def _parse_cards(self, html: str) -> Dict[str, dict]:
        """Parse every series card in a page of HTML."""
        tree = HTMLParser(html)
        cards: Dict[str, dict] = {}
        for anchor in tree.css('a[href^="/series/"]'):
            href = anchor.attributes.get("href", "")
            if "/chapter-" in href:
                continue
            slug = slug_from_href(href)
            if not slug or slug in cards:
                continue
            card = self._climb_to_card(anchor)
            if card is None:
                continue
            parsed = self._parse_card(card, slug, href)
            if parsed:
                cards[slug] = parsed
        return cards

    async def fetch_catalog(
        self, max_pages: int = MAX_BROWSE_PAGES, force: bool = False
    ) -> Dict[str, dict]:
        """Crawl /series/?page=N for the complete catalogue.

        This is the discovery source: it lists every series on the site,
        whereas the chapter sitemaps miss a handful of them.

        Cached like the chapter index, so backfilling several months in one
        run crawls the site once instead of once per month.
        """
        age = time.time() - self._catalog_at
        if self._catalog is not None and not force and age < CATALOG_CACHE_TTL:
            logger.info(f"Using cached catalog ({age:.0f}s old, {len(self._catalog)} series)")
            return self._catalog

        catalog: Dict[str, dict] = {}
        expected: Optional[int] = None

        async with self._client() as client:
            page = 1
            while page <= max_pages:
                try:
                    resp = await self._get(client, f"{BROWSE_URL}?page={page}")
                except httpx.HTTPError as e:
                    logger.error(f"Browse page {page} failed: {e}")
                    break

                if expected is None:
                    m = _FOUND_COUNT_RE.search(resp.text)
                    if m:
                        expected = int(m.group(1).replace(",", ""))

                found = self._parse_cards(resp.text)
                # Never stop on a page of duplicates: the site reorders as
                # chapters publish, so a series can shift between pages
                # mid-crawl. Only an empty page means we are past the end.
                if not found:
                    break
                catalog.update(found)
                page += 1
                await asyncio.sleep(REQUEST_DELAY)

        if expected and len(catalog) < expected:
            logger.warning(
                f"Catalog crawl saw {len(catalog)} of {expected} series the site reports; "
                "pagination shifted mid-crawl"
            )
        logger.info(f"Catalog crawled: {len(catalog)} series (site reports {expected})")
        self._catalog = catalog
        self._catalog_at = time.time()
        return catalog

    def _parse_card(self, card, slug: str, href: str) -> Optional[dict]:
        title = None
        for a in card.css('a[href^="/series/"]'):
            t = (a.attributes.get("title") or "").strip()
            if t:
                title = t
                break
        if not title:
            title = card.css_first("a[href^='/series/']").text(strip=True)
        if not title:
            return None

        cover = None
        for img in card.css("img"):
            src = img.attributes.get("src", "")
            if "storage.vortexscans.org" in src and "Logo" not in src:
                cover = src
                break

        rating = None
        score = card.css_first(".numscore")
        if score is not None:
            try:
                rating = float(score.text(strip=True))
            except ValueError:
                pass

        series_type = None
        status = None
        for span in card.css("span, p"):
            text = span.text(strip=True)
            low = text.lower()
            if series_type is None and low in KNOWN_TYPES:
                series_type = text
            elif status is None and low in KNOWN_STATUSES:
                status = text

        # Chapters shown on the card, with their exact release timestamps.
        card_chapters: List[dict] = []
        for a in card.css('a[href*="/chapter-"]'):
            chref = a.attributes.get("href", "")
            m = _CHAPTER_URL_RE.search(chref)
            if not m:
                continue
            tnode = a.css_first("time[datetime]")
            card_chapters.append(
                {
                    "number": m.group(2),
                    "url": BASE_URL + chref,
                    "released_at": _parse_ts(tnode.attributes.get("datetime", "")) if tnode else None,
                }
            )

        return {
            "slug": slug,
            "name": html_lib.unescape(title),
            "url": BASE_URL + href,
            "cover_url": cover,
            "rating": rating,
            "type": series_type,
            "status": status,
            "card_chapters": card_chapters,
        }

    # --------------------------------------------------------- chapter index

    async def fetch_chapter_index(
        self, force: bool = False
    ) -> Dict[str, List[Tuple[str, Optional[datetime]]]]:
        """Build slug -> [(chapter_number, released_at)] from the chapter sitemaps.

        Cached in memory for SITEMAP_CACHE_TTL seconds; the full parse is
        ~24 requests, so it should not run on every call.
        """
        age = time.time() - self._chapter_index_at
        if self._chapter_index is not None and not force and age < SITEMAP_CACHE_TTL:
            logger.info(f"Using cached chapter index ({age:.0f}s old)")
            return self._chapter_index

        async with self._client() as client:
            resp = await self._get(client, SITEMAP_INDEX)
            sitemaps = [u for u in _LOC_RE.findall(resp.text) if "chapter-sitemap" in u]

            semaphore = asyncio.Semaphore(SITEMAP_CONCURRENCY)

            async def fetch_one(url: str) -> str:
                async with semaphore:
                    await asyncio.sleep(REQUEST_DELAY)
                    resp = await self._get(client, url)
                    return resp.text

            pages = await asyncio.gather(*(fetch_one(u) for u in sitemaps))

        index: Dict[str, List[Tuple[str, Optional[datetime]]]] = {}
        total = 0
        for page in pages:
            for loc, mod in _LOC_MOD_RE.findall(page):
                m = _CHAPTER_URL_RE.search(loc)
                if not m:
                    continue
                slug = slug_from_href(m.group(1))
                index.setdefault(slug, []).append((m.group(2), _parse_ts(mod)))
                total += 1

        for slug in index:
            index[slug].sort(key=lambda pair: _chapter_sort_key(pair[0]))

        self._chapter_index = index
        self._chapter_index_at = time.time()
        logger.info(f"Chapter index built: {total} chapters across {len(index)} series")
        return index

    # ---------------------------------------------------------- series page

    # <h1> is used for field labels as well as the title, and the set of
    # labels varies per series (Author/Artist only appear when known).
    LABELS = (
        "Status", "Type", "Chapters", "Last update",
        "Author", "Artist", "Serialization", "Released", "Genres", "Rating",
    )

    async def fetch_series_page(self, slug: str) -> Optional[dict]:
        """Scrape one series page for name, cover, type, status and chapter count."""
        url = series_url(slug)
        try:
            async with self._client() as client:
                resp = await self._get(client, url)
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch series page {slug}: {e}")
            return None

        tree = HTMLParser(resp.text)

        # The cover image's alt text is the most reliable source for the name:
        # the page uses <h1> for field labels too, and that label set varies.
        name = None
        cover = None
        for img in tree.css("img"):
            alt = img.attributes.get("alt", "")
            src = img.attributes.get("src", "")
            if alt.startswith("Cover of") and "storage.vortexscans.org" in src:
                cover = src
                name = alt[len("Cover of"):].strip()
                break

        if not name:
            for node in tree.css("h1"):
                text = node.text(strip=True)
                if text and text not in self.LABELS:
                    name = text
                    break

        total_chapters = None
        raw_total = self._labelled_value(tree, "Chapters")
        if raw_total:
            digits = re.sub(r"[^0-9]", "", raw_total)
            total_chapters = int(digits) if digits else None

        # Chapter rows carry exact timestamps. The list is lazy-loaded, so only
        # the newest batch appears — but for a genuinely new series that batch
        # still includes chapter 1, which is what the month filter needs.
        page_chapters: List[Tuple[str, Optional[datetime]]] = []
        for a in tree.css('a[href*="/chapter-"]'):
            m = _CHAPTER_URL_RE.search(a.attributes.get("href", ""))
            tnode = a.css_first("time[datetime]")
            if m and tnode:
                page_chapters.append(
                    (m.group(2), _parse_ts(tnode.attributes.get("datetime", "")))
                )

        status = self._labelled_value(tree, "Status")
        return {
            "page_chapters": page_chapters,
            "slug": slug,
            "name": html_lib.unescape(name) if name else slug.replace("-", " ").title(),
            "url": url,
            "cover_url": cover,
            "type": self._labelled_value(tree, "Type"),
            "status": status.title() if status else None,
            "total_chapters": total_chapters,
        }

    def _labelled_value(self, tree: HTMLParser, label: str) -> Optional[str]:
        """Read the value next to a field label, e.g. 'Type' -> 'Manhwa'."""
        for node in tree.css("h1, h2, h3"):
            if node.text(strip=True) != label or node.parent is None:
                continue
            parts = [p.strip() for p in node.parent.text(separator="|", strip=True).split("|")]
            parts = [p for p in parts if p and p != label]
            if parts:
                return parts[0]
        return None

    # ------------------------------------------------------------- assembly

    def build_series(
        self,
        card: dict,
        chapters: List[Tuple[str, Optional[datetime]]],
    ) -> VortexSeries:
        """Combine card metadata with the full chapter list into one record."""
        chapter_models = [
            ChapterInfo(
                number=number,
                url=chapter_url(card["slug"], number),
                released_at=released,
            )
            for number, released in chapters
        ]

        dated = [c for c in chapter_models if c.released_at]
        # Must match the rule the month filter used, or a series gets stored
        # with a debut date that contradicts why it was selected. Chapters are
        # not always stamped in order, so "earliest chapter" is not "chapter 1".
        debut = self._debut_from([(c.number, c.released_at) for c in chapter_models])
        if debut is None and dated:
            debut = min(c.released_at for c in dated)
        newest = max(chapter_models, key=lambda c: _chapter_sort_key(c.number)) if chapter_models else None
        last_update = max((c.released_at for c in dated), default=None)

        # Prefer the timestamp shown on the card for the newest chapter; it is
        # the same value but comes straight from the rendered page.
        for cc in card.get("card_chapters", []):
            if newest and cc["number"] == newest.number and cc["released_at"]:
                newest.released_at = cc["released_at"]
                if last_update is None or cc["released_at"] > last_update:
                    last_update = cc["released_at"]

        return VortexSeries(
            slug=card["slug"],
            name=card["name"],
            url=card["url"],
            cover_url=card.get("cover_url"),
            rating=card.get("rating"),
            type=card.get("type"),
            status=card.get("status"),
            # The series page states a chapter count; fall back to what the
            # sitemap listed if that field was missing.
            total_chapters=card.get("total_chapters") or len(chapter_models),
            first_chapter_released_at=debut,
            last_updated_at=last_update,
            latest_chapter=newest.number if newest else None,
            latest_chapter_url=newest.url if newest else None,
            chapters=chapter_models,
            scraped_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _debut_from(
        chapters: List[Tuple[str, Optional[datetime]]],
    ) -> Optional[datetime]:
        """Pick the debut timestamp from a list of (chapter_number, released_at).

        Deliberately strict: only chapter 1 (or a numbering that starts at 0
        or 1.x) counts. Falling back to "earliest chapter I happen to know
        about" would date a long-running series by its newest chapters — a
        card only lists the latest three, so Chapter 149 would look like a
        debut. Anything unproven is left for the caller to resolve.
        """
        dated = [(n, t) for n, t in chapters if t]
        if not dated:
            return None

        exact = next((t for n, t in dated if n == "1"), None)
        if exact:
            return exact

        lowest_number, lowest_ts = min(dated, key=lambda pair: _chapter_sort_key(pair[0]))
        if _chapter_sort_key(lowest_number) <= 2:
            return lowest_ts
        return None

    # ------------------------------------------------------------ orchestration

    async def scrape_new_series(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        existing_slugs: Optional[set] = None,
        existing_titles: Optional[set] = None,
    ) -> Tuple[ScrapeResult, List[VortexSeries]]:
        """Find series from Latest Releases whose chapter 1 dropped in the target month.

        Defaults to the current UTC month. Anything already in `existing_slugs`
        is skipped before any extra work is done.
        """
        started = time.time()
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month
        existing_slugs = existing_slugs or set()
        result = ScrapeResult(month=f"{year:04d}-{month:02d}")

        # 1. Discovery. The catalogue is authoritative for *which* series exist;
        #    the sitemaps are authoritative for *when* each chapter landed.
        catalog = await self.fetch_catalog()
        index = await self.fetch_chapter_index()

        all_slugs = set(catalog) | set(index)
        result.candidates_seen = len(all_slugs)
        series_list: List[VortexSeries] = []

        # 2. Resolve each series' debut date from the cheapest source available.
        unresolved: List[str] = []
        debut: Dict[str, datetime] = {}

        for slug in all_slugs:
            ts = self._debut_from(index.get(slug, []))
            if ts is None:
                ts = self._debut_from(
                    [
                        (c["number"], c["released_at"])
                        for c in catalog.get(slug, {}).get("card_chapters", [])
                    ]
                )
            if ts is None:
                unresolved.append(slug)
            else:
                debut[slug] = ts

        # 3. Anything still unknown gets its own page fetched, so a new series
        #    missing from the sitemaps cannot slip through.
        if unresolved:
            logger.info(f"Resolving {len(unresolved)} series with no known debut date")
            for slug in unresolved:
                await asyncio.sleep(REQUEST_DELAY)
                page = await self.fetch_series_page(slug)
                if not page:
                    continue
                ts = self._debut_from(page.get("page_chapters", []))
                if ts:
                    debut[slug] = ts

        matched = [
            slug
            for slug, ts in debut.items()
            if ts.year == year and ts.month == month
        ]
        result.matched_month = len(matched)

        # 4. Skip what is already stored. Records are never rewritten: once a
        #    series is captured it stays as it was at that moment.
        todo = []
        for slug in sorted(matched):
            if slug in existing_slugs:
                result.already_stored += 1
                result.skipped_slugs.append(slug)
            else:
                todo.append(slug)

        if not todo:
            result.duration_seconds = round(time.time() - started, 2)
            logger.info(f"Nothing new for {result.month} ({result.already_stored} already stored)")
            return result, series_list

        # 5. Build a full record for each genuinely new series.
        seen_titles = set(existing_titles or set())
        for slug in todo:
            await asyncio.sleep(REQUEST_DELAY)
            page = await self.fetch_series_page(slug)
            if page is None:
                continue

            card = catalog.get(slug, {})
            if card.get("rating") is not None:
                page["rating"] = card["rating"]
            for field in ("type", "status", "cover_url"):
                if not page.get(field) and card.get(field):
                    page[field] = card[field]
            page.setdefault("card_chapters", card.get("card_chapters", []))

            # Same comic under a second slug would otherwise be stored twice.
            key = normalize_title(page["name"])
            if key in seen_titles:
                result.already_stored += 1
                result.skipped_slugs.append(slug)
                logger.info(f"Skipping duplicate title: {page['name']} ({slug})")
                continue
            seen_titles.add(key)

            chapters = index.get(slug) or page.get("page_chapters", [])
            series_list.append(self.build_series(page, chapters))

        result.duration_seconds = round(time.time() - started, 2)
        logger.info(
            "Scrape complete: "
            f"{result.candidates_seen} seen, {result.matched_month} matched {result.month}, "
            f"{result.already_stored} already stored, {len(series_list)} new"
        )
        return result, series_list


    # -------------------------------------------------------- run and store

    async def run_and_store(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
        dry_run: bool = False,
    ) -> ScrapeResult:
        """Scrape and persist in one call.

        Shared by the API endpoint and the scheduled job so both behave
        identically. Series already in Firestore are never re-fetched.
        """
        from starlette.concurrency import run_in_threadpool

        from app.services.firebase import firebase_service

        existing = await run_in_threadpool(firebase_service.existing_ids, COLLECTION)
        stored_names = await run_in_threadpool(
            firebase_service.existing_values, COLLECTION, "name"
        )
        result, new_series = await self.scrape_new_series(
            year=year,
            month=month,
            existing_slugs=existing,
            existing_titles={normalize_title(n) for n in stored_names},
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
                logger.info(f"Saved series: {series.slug}")
            except Exception as e:
                logger.error(f"Failed to save {series.slug}: {e}")

        self.last_result = result
        self.last_run_at = datetime.now(timezone.utc)
        return result


vortexscans_scraper = VortexScansScraper()
