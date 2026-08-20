"""Cloudflare-protected fetching for sources that sit behind a JS challenge.

Why this exists
---------------
en-thunderscans.com sits behind Cloudflare, and its protection level moves. At
its strictest every plain HTTP client — httpx, curl, cloudscraper, even
TLS-impersonating clients — gets the "Just a moment..." interstitial with a 403
instead of content, and so does every datacenter proxy. That silently reduced
the Thunder scraper to zero results for a week, and it also breaks cover images
in the browser, because the covers are hotlinked straight from that origin.

How it works
------------
Plain httpx first, always. When the protection is off — which is most of the
time — that is the whole story, and no browser is ever started.

When a request comes back 403 or 503, the challenge is solved once by a real
(non-headless) Chrome driven through nodriver, which is patched enough not to
trip Cloudflare's automation checks. The browser is not used for the scraping
itself; that would be hopeless for 900 sitemap entries. It is used for the
`cf_clearance` cookie the challenge hands back, which — paired with the exact
User-Agent that earned it — makes an ordinary httpx client acceptable again, so
all the real work stays on the fast path.

`cf_clearance` is bound to the IP and the User-Agent that solved the challenge,
so both are captured together and refreshed together. The cookie is treated as
opaque and short-lived: rather than trusting a TTL, a 403 triggers exactly one
re-solve and retry.

Requirements
------------
Chrome must be reachable, and it must run headed — a headless Chrome announces
itself in the User-Agent and gets challenged forever. On a Linux host that means
an X server; the Docker image starts Xvfb for this reason.
"""

import asyncio
import time
from typing import Dict, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Used until a solved session replaces it with the browser's real one. A plain
# scraper UA is enough while the protection is off, but a browser-shaped one
# draws less attention when it tightens.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# How long a solved clearance is trusted before the next 403 is allowed to
# refresh it rather than being retried against a stale cookie.
SESSION_TTL = 1800

# Upper bound on how long to sit on the interstitial before giving up.
CHALLENGE_TIMEOUT = 60

# Statuses Cloudflare answers a blocked request with.
BLOCKED_STATUSES = (403, 503)


class ChallengeError(RuntimeError):
    """Raised when the Cloudflare challenge could not be solved.

    Deliberately loud: a scraper that cannot reach its source must fail
    visibly, not report a successful run that found nothing.
    """


class CloudflareSession:
    """An httpx front end for one origin, with a browser behind it as fallback."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._cookies: Dict[str, str] = {}
        self._user_agent: str = DEFAULT_USER_AGENT
        self._solved_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- solving

    @property
    def cleared(self) -> bool:
        """True while a recently solved clearance is worth reusing."""
        return bool(self._cookies) and (time.time() - self._solved_at) < SESSION_TTL

    async def ensure(self, force: bool = False) -> None:
        """Get a clearance cookie, unless a fresh one is already in hand."""
        if self.cleared and not force:
            return
        async with self._lock:
            # Another caller may have solved it while this one waited on the
            # lock; without this check a burst of 403s starts a browser each.
            if self.cleared and not force:
                return
            await self._solve()

    async def _solve(self) -> None:
        try:
            import nodriver as uc
        except ImportError as e:  # pragma: no cover - deployment problem
            raise ChallengeError(
                "nodriver is not installed, so the Cloudflare challenge on "
                f"{self.base_url} cannot be solved"
            ) from e

        logger.info(f"Solving Cloudflare challenge for {self.base_url}")
        started = time.time()
        browser = None
        try:
            kwargs = {"headless": False}
            if settings.chrome_path:
                kwargs["browser_executable_path"] = settings.chrome_path
                # An explicit path means the containerised Chromium, which
                # cannot use its sandbox as an unprivileged user in Docker.
                kwargs["sandbox"] = False
            browser = await uc.start(**kwargs)
            page = await browser.get(f"{self.base_url}/")

            deadline = time.time() + CHALLENGE_TIMEOUT
            challenged = True
            jar: Dict[str, str] = {}
            while time.time() < deadline:
                await asyncio.sleep(2)
                challenged = "Just a moment" in await page.get_content()
                jar = self._jar_from(await browser.cookies.get_all())
                # Cloudflare sets the cookie a moment after the interstitial
                # clears, so keep waiting for it rather than leaving as soon as
                # the page turns real.
                if not challenged and "cf_clearance" in jar:
                    break

            user_agent = await page.evaluate("navigator.userAgent")
        except ChallengeError:
            raise
        except Exception as e:
            logger.exception(f"Challenge browser failed for {self.base_url}")
            # Some failures here carry no message at all, so name the type too.
            raise ChallengeError(
                f"Challenge browser failed for {self.base_url}: "
                f"{type(e).__name__}: {e}"
            ) from e
        finally:
            if browser is not None:
                try:
                    browser.stop()
                except Exception:
                    pass

        if challenged:
            raise ChallengeError(
                f"Still on the interstitial after {time.time() - started:.0f}s on "
                f"{self.base_url}. Chrome must run headed, and the host IP must "
                "not be blocked outright by Cloudflare."
            )

        self._cookies = jar
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._solved_at = time.time()

        if "cf_clearance" in jar:
            logger.info(
                f"Cloudflare challenge solved for {self.base_url} in "
                f"{time.time() - started:.1f}s ({len(jar)} cookies)"
            )
        else:
            # The browser was let through without being challenged at all. That
            # happens whenever Cloudflare relaxes, and it leaves nothing to
            # carry over to httpx — so if httpx is still being blocked, the
            # retry above it will fail and say so.
            logger.warning(
                f"No challenge was presented on {self.base_url}, so there is no "
                "cf_clearance to reuse. Keeping the browser's identity anyway."
            )

    def _jar_from(self, cookies) -> Dict[str, str]:
        host = httpx.URL(self.base_url).host or ""
        # Cookie domains are stored with a leading dot, hence the suffix test.
        return {
            c.name: c.value
            for c in cookies
            if c.domain and host.endswith(c.domain.lstrip("."))
        }

    # ------------------------------------------------------------ fetching

    def _headers(self) -> Dict[str, str]:
        return {
            # Must match the UA that solved the challenge, or the cookie is void.
            "User-Agent": self._user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

    def client(self) -> httpx.AsyncClient:
        """An httpx client carrying whatever identity the session currently has.

        Held only for the length of one batch of requests, so a refresh in the
        middle cannot leave a client behind with stale cookies.
        """
        return httpx.AsyncClient(
            headers=self._headers(),
            cookies=self._cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0),
        )

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """GET plainly, falling back to a browser-solved clearance on a block."""
        async with self.client() as client:
            response = await client.get(url, **kwargs)

        if response.status_code not in BLOCKED_STATUSES:
            return response

        logger.info(f"Cloudflare blocked {url} ({response.status_code}), solving")
        await self.ensure(force=True)
        async with self.client() as client:
            return await client.get(url, **kwargs)


thunderscans_session = CloudflareSession("https://en-thunderscans.com")
