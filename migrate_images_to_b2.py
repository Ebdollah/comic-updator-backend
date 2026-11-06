"""Migration script: Download existing comic covers from source websites and store them in Backblaze B2.

Usage:
    python migrate_images_to_b2.py [--source all|vortexscans|thunderscans] [--limit N] [--dry-run]
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.services.b2_storage import b2_storage
from app.services.cloudflare import thunderscans_session
from app.services.firebase import firebase_service
from app.services.series_index import series_index
from app.services.sources import SOURCES

logger = get_logger("migrate_images")


async def migrate_comic(
    doc_id: str,
    data: dict,
    collection: str,
    source_id: str,
    dry_run: bool = False,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict[str, Any]:
    """Process a single comic: download cover, upload to B2, update Firestore."""
    async with semaphore if semaphore else asyncio.nullcontext():
        name = data.get("name", doc_id)
        cover_url = data.get("cover_url")

        if not cover_url:
            return {"status": "skipped", "reason": "no_cover", "doc_id": doc_id, "name": name}

        if "backblazeb2.com" in cover_url:
            return {"status": "skipped", "reason": "already_b2", "doc_id": doc_id, "name": name}

        if dry_run:
            logger.info(f"[DRY-RUN] Would migrate '{name}': {cover_url}")
            return {
                "status": "dry_run",
                "doc_id": doc_id,
                "name": name,
                "old_url": cover_url,
            }

        session = thunderscans_session if source_id == "thunderscans" else None
        b2_url = await b2_storage.download_and_store_image(
            image_url=cover_url,
            source_id=source_id,
            slug=doc_id,
            session=session,
        )

        if not b2_url or b2_url == cover_url:
            logger.warning(f"Failed to migrate cover for '{name}' ({doc_id})")
            return {"status": "failed", "doc_id": doc_id, "name": name, "url": cover_url}

        # Update Firestore
        updates = {
            "cover_url": b2_url,
            "original_cover_url": cover_url,
        }
        await run_in_threadpool(
            firebase_service.set_document,
            collection,
            doc_id,
            updates,
            merge=True,
        )

        logger.info(f"Migrated '{name}' -> {b2_url}")
        return {
            "status": "migrated",
            "doc_id": doc_id,
            "name": name,
            "old_url": cover_url,
            "new_url": b2_url,
        }


async def run_migration(
    source_choice: str = "all",
    limit: Optional[int] = None,
    dry_run: bool = False,
    concurrency: int = 5,
):
    print("=" * 60)
    print("Comic Covers -> Backblaze B2 Migration")
    print(f"Source: {source_choice} | Limit: {limit or 'None'} | Dry Run: {dry_run}")
    print("=" * 60)

    # 1. Test B2 connection
    print("Checking Backblaze B2 connection...")
    b2_check = await b2_storage.test_connection()
    if not b2_check.get("ok"):
        print(f"Error: Backblaze B2 connection failed: {b2_check.get('error')}")
        sys.exit(1)
    print(f"Backblaze B2 OK: Bucket = {b2_check.get('bucket')} | Download URL = {b2_check.get('download_url')}")

    # 2. Check Firebase
    print("Initializing Firebase...")
    firebase_service.initialize()
    if firebase_service.db is None:
        print("Error: Firebase Firestore could not be initialized")
        sys.exit(1)
    print("Firebase OK.")

    # 3. Determine sources to migrate
    sources_to_run = (
        [SOURCES[source_choice]]
        if source_choice in SOURCES
        else list(SOURCES.values())
    )

    semaphore = asyncio.Semaphore(concurrency)
    overall_start = time.monotonic()
    total_migrated = 0
    total_already = 0
    total_skipped = 0
    total_failed = 0

    for src in sources_to_run:
        print(f"\nProcessing source: {src.name} (collection: {src.collection})")
        col_ref = firebase_service.get_collection(src.collection)
        docs = list(col_ref.stream())
        print(f"Found {len(docs)} documents in {src.collection}")

        if limit and limit > 0:
            docs = docs[:limit]
            print(f"Limiting to first {limit} documents")

        tasks = [
            migrate_comic(
                doc_id=doc.id,
                data=doc.to_dict() or {},
                collection=src.collection,
                source_id=src.id,
                dry_run=dry_run,
                semaphore=semaphore,
            )
            for doc in docs
        ]

        results = await asyncio.gather(*tasks)

        migrated = sum(1 for r in results if r["status"] == "migrated")
        already = sum(1 for r in results if r.get("reason") == "already_b2")
        no_cover = sum(1 for r in results if r.get("reason") == "no_cover")
        failed = sum(1 for r in results if r["status"] == "failed")
        dry = sum(1 for r in results if r["status"] == "dry_run")

        total_migrated += migrated
        total_already += already
        total_skipped += no_cover
        total_failed += failed

        print(f"--- Summary for {src.name} ---")
        if dry_run:
            print(f"  Dry run targets: {dry}")
        else:
            print(f"  Migrated: {migrated}")
        print(f"  Already in B2: {already}")
        print(f"  No cover: {no_cover}")
        print(f"  Failed: {failed}")

        # Invalidate in-memory index
        series_index.invalidate(src.collection)

    duration = round(time.monotonic() - overall_start, 2)
    print("\n" + "=" * 60)
    print(f"Migration Complete in {duration}s!")
    print(f"Total Migrated: {total_migrated}")
    print(f"Already in B2: {total_already}")
    print(f"No Cover / Skipped: {total_skipped}")
    print(f"Failed: {total_failed}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Migrate comic covers to Backblaze B2")
    parser.add_argument(
        "--source",
        choices=["all", "vortexscans", "thunderscans"],
        default="all",
        help="Source to migrate",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of comics to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without uploading or saving")
    parser.add_argument(
        "--concurrency", type=int, default=5, help="Number of concurrent downloads/uploads"
    )

    args = parser.parse_args()
    asyncio.run(
        run_migration(
            source_choice=args.source,
            limit=args.limit,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
