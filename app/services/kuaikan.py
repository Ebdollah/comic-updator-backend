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

Text is stored in English: tags and status through fixed tables, the title
through DeepL, with the Chinese kept in `name_original`/`genres_original`.
Theme filtering, the BL/GL filter and duplicate checks work on the Chinese. Covers go to the
B2 bucket like the other sources.
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
from app.services.translation import TranslationError, get_translator, title_case
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

# BL/GL content is never stored from this source, whatever its other themes.
# Chinese platforms rarely say so outright: BL is usually coded as 双男主
# (two male leads) or 纯爱 ("pure love"), GL as 双女主 or 百合. A tag is
# excluded when it is one of these or contains one of the fragments, so
# variants like 耽美向 hit too.
BLGL_TAGS = {
    "耽美", "纯爱", "双男主", "男男", "腐", "腐向", "腐女", "攻受", "男同", "耽改", "bl",
    "百合", "双女主", "女女", "女同", "姬情", "gl",
}
BLGL_TAG_FRAGMENTS = ("耽美", "纯爱", "双男主", "男男", "腐向", "耽改", "百合", "双女主", "女女", "女同")

# Description and title are free text, so only fragments that cannot be part
# of an ordinary phrase are searched there. 男男/女女 are left out: they occur
# in 男男女女 ("men and women") and across word joins like 暖男男主.
BLGL_TEXT_FRAGMENTS = (
    "耽美", "纯爱", "双男主", "腐向", "腐女", "攻受", "男同", "耽改",
    "百合", "双女主", "女同", "姬情",
)
_BLGL_TEXT_IGNORE = ("百合花",)  # the flower
_BLGL_LATIN = re.compile(
    r"(?<![A-Za-z])(?:BL|GL|(?i:boys'? ?love|girls'? ?love|yaoi|yuri|shounen[- ]ai|shoujo[- ]ai))(?![A-Za-z])"
)

STATUS_LABELS = {"连载中": "Ongoing", "连载": "Ongoing", "已完结": "Completed", "完结": "Completed"}

# English names for the site's tags. A fixed table rather than machine
# translation, so a tag reads the same on every series. Tags missing here
# still get translated by DeepL, and are logged so they can be added.
THEMES_EN: Dict[str, str] = {
    # The filter bar's themes
    "恋爱": "Romance", "古风": "Ancient", "大女主": "Female Lead", "穿越": "Time Travel",
    "校园": "School", "玄幻": "Xuanhuan", "总裁": "CEO", "悬疑": "Mystery",
    "非人类": "Non-Human", "恐怖": "Horror", "异能": "Superpowers", "奇幻": "Fantasy",
    "系统": "System", "重生": "Rebirth", "末世": "Apocalypse", "都市": "Urban",
    "热血": "Action", "剧情": "Drama", "萌系": "Cute", "搞笑": "Comedy",
    "武侠": "Wuxia", "正能量": "Positive Energy",
    # Other tags series carry
    "逆袭": "Comeback", "青春": "Youth", "治愈": "Healing", "唯美": "Aesthetic",
    "古言": "Historical Romance", "原创": "Original", "双男主": "Two Male Leads",
    "复仇": "Revenge", "游戏": "Game", "幻想": "Fantasy", "动作": "Action",
    "少年": "Shounen", "少女": "Shoujo", "冒险": "Adventure", "宫斗": "Palace Intrigue",
    "宅斗": "Household Intrigue", "日常": "Slice of Life", "修仙": "Cultivation",
    "修真": "Cultivation", "仙侠": "Xianxia", "科幻": "Sci-Fi", "灵异": "Supernatural",
    "推理": "Detective", "美食": "Cooking", "运动": "Sports", "竞技": "Competition",
    "职场": "Workplace", "家庭": "Family", "甜宠": "Sweet Romance", "霸总": "CEO",
    "历史": "Historical", "后宫": "Harem", "魔幻": "Magic", "机甲": "Mecha",
    "丧尸": "Zombies", "无限流": "Infinite Flow", "种田": "Farming", "爆笑": "Comedy",
    "励志": "Inspirational", "网游": "Online Game", "豪门": "Wealthy Family",
    "宫廷": "Palace", "江湖": "Martial World", "神豪": "Tycoon", "兽人": "Beastmen",
    "战斗": "Battle", "怪物": "Monsters", "魔法": "Magic", "御兽": "Beast Taming",
}

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


def blgl_signals(tags: List[str], *texts: Optional[str]) -> List[str]:
    """Every BL/GL signal in a series' tags, title and description.

    Any one of them excludes the series; the list is for the log.
    """
    found: List[str] = []
    for tag in tags or []:
        key = unicodedata.normalize("NFKC", tag or "").strip().casefold()
        if key in BLGL_TAGS or any(f in key for f in BLGL_TAG_FRAGMENTS) or _BLGL_LATIN.search(tag or ""):
            found.append(f"tag {tag}")
    for text in texts:
        text = unicodedata.normalize("NFKC", text or "")
        for ignore in _BLGL_TEXT_IGNORE:
            text = text.replace(ignore, "")
        found += [f"text '{f}'" for f in BLGL_TEXT_FRAGMENTS if f in text]
        found += [f"text '{m.group(0)}'" for m in _BLGL_LATIN.finditer(text)]
    return found


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

    # ---------------------------------------------------------- translation

    async def english(self, title: str, tags: List[str]) -> Tuple[Optional[str], List[str]]:
        """English title (None when translation failed) and English tags."""
        unknown = [t for t in tags if t not in THEMES_EN]
        translated: Dict[str, str] = {}
        name_en: Optional[str] = None
        translator = get_translator()
        if translator is None:
            logger.warning("DEEPL_KEY is not set; Kuaikan titles stay in Chinese")
        else:
            try:
                out = await translator.translate([title] + unknown)
                name_en = title_case(out[0]) or None
                translated = {zh: title_case(en) for zh, en in zip(unknown, out[1:])}
                if unknown:
                    logger.warning(f"Kuaikan tags not in THEMES_EN, machine-translated: {translated}")
            except TranslationError as e:
                logger.error(f"Could not translate kuaikan title {title!r}: {e}")
        # Tags that only DeepL could do, but failed to, stay in Chinese.
        genres = _merge_tags([THEMES_EN.get(t) or translated.get(t) or t for t in tags])
        return name_en, genres

    async def apply_english(self, series: VortexSeries) -> None:
        """Swap a freshly built series' Chinese text for English, keeping the original.

        If the title cannot be translated it stays Chinese, and
        translate_stored picks it up again on the next run.
        """
        series.name_original = series.name
        series.genres_original = list(series.genres)
        name_en, series.genres = await self.english(series.name, series.genres)
        if name_en:
            series.name = name_en

    async def translate_stored(self) -> int:
        """Translate stored series whose title is still Chinese; returns how many.

        Covers series saved before translation existed, and any whose
        translation failed when they were saved. A translated series has a
        `name_original` that differs from its `name`, and is left alone.
        """
        from starlette.concurrency import run_in_threadpool

        from app.services.firebase import firebase_service

        if get_translator() is None:
            return 0
        docs = await run_in_threadpool(
            firebase_service.list_projected,
            COLLECTION,
            ["slug", "name", "name_original", "genres", "genres_original"],
        )
        updated = 0
        for doc in docs:
            original = doc.get("name_original")
            if original and doc.get("name") != original:
                continue
            title = original or doc.get("name") or ""
            tags = doc.get("genres_original") or doc.get("genres") or []
            name_en, genres_en = await self.english(title, tags)
            if not name_en or not doc.get("slug"):
                continue
            await run_in_threadpool(
                firebase_service.set_document,
                COLLECTION,
                doc["slug"],
                {"name": name_en, "name_original": title, "genres": genres_en, "genres_original": tags},
                merge=True,
            )
            updated += 1
            logger.info(f"Translated kuaikan {doc['slug']}: {title} -> {name_en}")
        return updated

    # -------------------------------------------------------- orchestration

    @staticmethod
    def _excluded(slug: str, title: Optional[str], tags: List[str], *texts: Optional[str]) -> bool:
        """Apply this source's content filters, logging why a series is dropped."""
        themes = excluded_themes_in(tags)
        if themes:
            logger.info(f"Skipping kuaikan {slug} {title}: excluded theme(s) {themes}")
            return True
        signals = blgl_signals(tags, *texts)
        if signals:
            logger.info(f"Skipping kuaikan {slug} {title}: BL/GL {signals}")
            return True
        return False

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
                list_tags = entry.get("category") or []
                if self._excluded(slug, entry.get("title"), list_tags, entry.get("title")):
                    continue

                await asyncio.sleep(REQUEST_DELAY)
                page = await self.fetch_series_page(client, entry["topic_id"])
                info = (page or {}).get("info") or {}
                # The page has more tags than the list, and the description.
                # Series not viewable on the web have neither; the list tags
                # and title above are all there is to go on for those.
                if self._excluded(
                    slug, entry.get("title"), info.get("tags") or [], info.get("title"), info.get("description")
                ):
                    continue

                series = self.build_series(entry, page)
                key = normalize_title(series.name)
                if key and key in seen_titles:
                    logger.info(f"Skipping duplicate kuaikan title: {series.name} ({slug})")
                    result.already_stored += 1
                    result.skipped_slugs.append(slug)
                    continue
                seen_titles.add(key)
                await self.apply_english(series)
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
        # Duplicates are judged on the Chinese title, which a stored series
        # keeps in name_original once its name is English.
        names = await run_in_threadpool(firebase_service.existing_values, COLLECTION, "name_original")
        names |= await run_in_threadpool(firebase_service.existing_values, COLLECTION, "name")
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

        try:
            retried = await self.translate_stored()
            if retried:
                logger.info(f"Translated {retried} stored kuaikan series still in Chinese")
        except Exception as e:
            logger.error(f"Kuaikan translation retry failed: {e}")

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
