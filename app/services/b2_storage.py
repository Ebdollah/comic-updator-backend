import asyncio
import hashlib
import mimetypes
import re
from typing import Any, Optional, Tuple
from urllib.parse import quote, unquote

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


class B2StorageService:
    """Service for uploading and serving images using Backblaze B2 storage."""

    def __init__(self) -> None:
        self._auth_token: Optional[str] = None
        self._api_url: Optional[str] = None
        self._download_url: Optional[str] = None
        self._upload_pool: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """True if all required Backblaze B2 credentials are present."""
        return bool(
            settings.b2_key_id
            and settings.b2_application_key
            and settings.b2_bucket_id
            and settings.b2_bucket_name
        )

    @property
    def bucket_name(self) -> str:
        return settings.b2_bucket_name or ""

    @property
    def bucket_id(self) -> str:
        return settings.b2_bucket_id or ""

    @property
    def download_url(self) -> str:
        if settings.b2_download_url:
            return settings.b2_download_url.rstrip("/")
        return (self._download_url or "https://f005.backblazeb2.com").rstrip("/")

    async def authorize(self, force: bool = False) -> dict:
        """Authenticate with Backblaze B2 and cache auth credentials."""
        if not self.is_configured:
            raise RuntimeError("Backblaze B2 is not configured in settings")

        if self._auth_token and self._api_url and not force:
            return {
                "apiUrl": self._api_url,
                "downloadUrl": self._download_url,
                "authorizationToken": self._auth_token,
            }

        async with self._lock:
            if self._auth_token and self._api_url and not force:
                return {
                    "apiUrl": self._api_url,
                    "downloadUrl": self._download_url,
                    "authorizationToken": self._auth_token,
                }

            auth_url = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    auth_url,
                    auth=(settings.b2_key_id, settings.b2_application_key),
                )
                resp.raise_for_status()
                data = resp.json()

            storage_api = data.get("apiInfo", {}).get("storageApi", {})
            self._api_url = storage_api.get("apiUrl")
            self._download_url = storage_api.get("downloadUrl")
            self._auth_token = data.get("authorizationToken")

            logger.info(
                f"Backblaze B2 authorized successfully. API: {self._api_url}, "
                f"Download: {self.download_url}"
            )
            return data

    async def get_upload_url(
        self, force: bool = False, force_new: bool = False
    ) -> Tuple[str, str]:
        """Obtain an upload URL and token for the target bucket.

        Uses an internal pool so concurrent uploads don't share the same upload token.
        """
        if not force and not force_new:
            try:
                return self._upload_pool.get_nowait()
            except asyncio.QueueEmpty:
                pass

        await self.authorize()
        get_url_endpoint = f"{self._api_url}/b2api/v3/b2_get_upload_url"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                get_url_endpoint,
                headers={"Authorization": self._auth_token},
                json={"bucketId": self.bucket_id},
            )
            if resp.status_code == 401:
                # Token might have expired, re-authorize once
                await self.authorize(force=True)
                resp = await client.post(
                    f"{self._api_url}/b2api/v3/b2_get_upload_url",
                    headers={"Authorization": self._auth_token},
                    json={"bucketId": self.bucket_id},
                )
            resp.raise_for_status()
            upload_data = resp.json()

        return upload_data["uploadUrl"], upload_data["authorizationToken"]

    def _return_upload_url(self, upload_url: str, upload_token: str) -> None:
        """Return a successfully used upload URL to the pool for reuse."""
        if self._upload_pool.qsize() < 10:
            self._upload_pool.put_nowait((upload_url, upload_token))

    async def upload_bytes(
        self,
        content: bytes,
        content_type: str,
        file_path: str,
        max_retries: int = 3,
    ) -> str:
        """Upload raw bytes to Backblaze B2 and return the public URL."""
        if not self.is_configured:
            raise RuntimeError("Backblaze B2 is not configured")

        clean_path = file_path.lstrip("/")
        # URL encode filename for header, keeping slashes safe
        encoded_file_name = quote(clean_path, safe="/-_.~")
        content_sha1 = hashlib.sha1(content).hexdigest()
        content_length = str(len(content))

        for attempt in range(max_retries):
            upload_url, upload_token = await self.get_upload_url(force_new=(attempt > 0))
            headers = {
                "Authorization": upload_token,
                "X-Bz-File-Name": encoded_file_name,
                "Content-Type": content_type,
                "Content-Length": content_length,
                "X-Bz-Content-Sha1": content_sha1,
            }

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(upload_url, headers=headers, content=content)

                if resp.status_code == 200:
                    public_url = f"{self.download_url}/file/{self.bucket_name}/{clean_path}"
                    self._return_upload_url(upload_url, upload_token)
                    logger.debug(f"Uploaded to B2: {public_url}")
                    return public_url

                logger.warning(
                    f"B2 upload attempt {attempt + 1} returned status {resp.status_code}: {resp.text[:200]}"
                )

            except Exception as e:
                logger.warning(f"B2 upload attempt {attempt + 1} failed: {e}")

            await asyncio.sleep(0.5 * (attempt + 1))

        raise RuntimeError(f"Failed to upload {file_path} to Backblaze B2 after {max_retries} attempts")

    async def download_and_store_image(
        self,
        image_url: str,
        source_id: str,
        slug: str,
        session: Optional[Any] = None,
    ) -> Optional[str]:
        """Fetch an image from upstream and store it in Backblaze B2.

        Returns the permanent public B2 URL, or None if download/upload fails.
        """
        if not image_url:
            return None

        # If already stored in Backblaze, no need to re-upload
        if "backblazeb2.com" in image_url or (self._download_url and self._download_url in image_url):
            return image_url

        if not self.is_configured:
            logger.warning("B2 is not configured; returning original image URL")
            return image_url

        try:
            # 1. Fetch image bytes from source
            content: bytes
            content_type: str = "image/jpeg"

            if session is not None:
                resp = await session.get(image_url)
                resp.raise_for_status()
                content = resp.content
                content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            else:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    ),
                }
                if "vortexscans.org" in image_url:
                    headers["Referer"] = "https://vortexscans.org/"
                elif "en-thunderscans.com" in image_url:
                    headers["Referer"] = "https://en-thunderscans.com/"

                async with httpx.AsyncClient(
                    headers=headers, follow_redirects=True, timeout=30.0
                ) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    content = resp.content
                    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

            if not content_type.startswith("image/"):
                logger.warning(
                    f"Skipping upload for {slug}: upstream returned non-image content-type {content_type}"
                )
                return image_url

            # 2. Determine file extension
            ext = MIME_TO_EXT.get(content_type)
            if not ext:
                ext = mimetypes.guess_extension(content_type) or ".jpg"

            # 3. Create sanitized file key
            clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", unquote(slug)).strip("_")
            file_path = f"covers/{source_id}/{clean_slug}{ext}"

            # 4. Upload to B2
            public_url = await self.upload_bytes(content, content_type, file_path)
            return public_url

        except Exception as e:
            logger.error(f"Failed to download and store cover for {slug} ({image_url}): {e}")
            return image_url

    async def test_connection(self) -> dict:
        """Test B2 connection and bucket access."""
        try:
            auth_info = await self.authorize(force=True)
            upload_url, _ = await self.get_upload_url(force=True)
            return {
                "ok": True,
                "bucket": self.bucket_name,
                "bucket_id": self.bucket_id,
                "api_url": self._api_url,
                "download_url": self.download_url,
                "upload_url_ready": bool(upload_url),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }


b2_storage = B2StorageService()
