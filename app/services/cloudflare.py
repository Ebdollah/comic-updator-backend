"""Cloudflare-protected fetching for sources that sit behind a JS challenge.

Why this exists
---------------
en-thunderscans.com went behind Cloudflare's "Just a moment..." interstitial.
Every plain HTTP client — httpx, curl, cloudscraper, and even TLS-impersonating
clients — gets a 403 challenge page instead of content, and so does every
datacenter proxy. That silently reduced the Thunder scraper to zero results and
also broke cover images in the browser, because the covers are hotlinked
straight from the protected origin.

How it works
------------
The challenge is solved once by a real (non-headless) Chrome driven through
nodriver, which is patched enough not to trip Cloudflare's automation checks.
The point is not to keep scraping in a browser — that would be far too slow for
900 sitemap entries. The point is the `cf_clearance` cookie the challenge hands
back. Paired with the exact User-Agent that earned it, that cookie makes an
ordinary httpx client acceptable to Cloudflare, so all the real work stays on
the fast path.

`cf_clearance` is bound to the IP and the User-Agent that solved the challenge,
so both are captured together and refreshed together. The cookie is treated as
opaque and short-lived: rather than trusting a TTL, any 403 triggers exactly one
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

# How long a solved session is reused before being refreshed proactively. The
# cookie usually outlives this; a 403 refresh is the real safety net.
SESSION_TTL = 1800

# Upper bound on how long to sit on the interstitial before giving up.
CHALLENGE_TIMEOUT = 60


class ChallengeError(RuntimeError):
    """Raised when the Cloudflare challenge could not be solved.

    Deliberately loud: a scraper that cannot reach its source must fail
    visibly, not report a successful run that found nothing.
    """


class CloudflareSession:
    """A `cf_clearance`-backed httpx client for one protected origin."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._cookies: Dict[str, str] = {}
        self._user_agent: Optional[str] = None
        self._solved_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- solving

    @property
    def _expired(self) -> bool:
        return not self._cookies or (time.time() - self._solved_at) > SESSION_TTL

    async def ensure(self, force: bool = False) -> None:
        """Make sure a usable clearance cookie is in hand."""
        if not (force or self._expired):
            return
        async with self._lock:
            # Another caller may have solved it while this one waited.
            if not force and not self._expired:
                return
            # Cloudflare sometimes serves a slower challenge that outlasts the
            # wait below; a second attempt with a fresh browser usually clears
            # in seconds, so one retry is worth more than a longer timeout.
            try:
                await self._solve()
            except ChallengeError as first:
                logger.info(f"Challenge attempt failed ({first}); retrying once")
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
            while time.time() < deadline:
                await asyncio.sleep(2)
                html = await page.get_content()
                if "Just a moment" not in html:
                    break

            user_agent = await page.evaluate("navigator.userAgent")
            cookies = await browser.cookies.get_all()
        except ChallengeError:
            raise
        except Exception as e:
            raise ChallengeError(f"Challenge browser failed for {self.base_url}: {e}") from e
        finally:
            if browser is not None:
                try:
                    browser.stop()
                except Exception:
                    pass

        host = httpx.URL(self.base_url).host
        # Cookie domains are stored with a leading dot, hence the suffix test.
        jar = {
            c.name: c.value
            for c in cookies
            if c.domain and host.endswith(c.domain.lstrip("."))
        }
        if "cf_clearance" not in jar:
            raise ChallengeError(
                f"No cf_clearance cookie after {time.time() - started:.0f}s on "
                f"{self.base_url}. Chrome must run headed, and the host IP must "
                "not be blocked outright by Cloudflare."
            )

        self._cookies = jar
        self._user_agent = user_agent
        self._solved_at = time.time()
        logger.info(
            f"Cloudflare challenge solved for {self.base_url} in "
            f"{time.time() - started:.1f}s ({len(jar)} cookies)"
        )

    # ------------------------------------------------------------ fetching

    def _headers(self) -> Dict[str, str]:
        return {
            # Must match the UA that solved the challenge, or the cookie is void.
            "User-Agent": self._user_agent or "",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def client(self) -> httpx.AsyncClient:
        """An httpx client carrying the current clearance.

        Call `ensure()` first. Held only for the length of one batch of
        requests, so a mid-batch refresh cannot leave a client behind with
        stale cookies.
        """
        return httpx.AsyncClient(
            headers=self._headers(),
            cookies=self._cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0),
        )

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """GET through the clearance, re-solving once if it has gone stale."""
        await self.ensure()
        async with self.client() as client:
            response = await client.get(url, **kwargs)

        if response.status_code in (403, 503):
            logger.info(f"Clearance rejected on {url}, re-solving")
            await self.ensure(force=True)
            async with self.client() as client:
                response = await client.get(url, **kwargs)

        return response


thunderscans_session = CloudflareSession("https://en-thunderscans.com")
