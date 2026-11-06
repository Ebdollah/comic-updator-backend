"""Backfill historical comic data by month (e.g., Jan-May 2026) for Vortex Scans and Thunder Scans.

Extracts comics that debuted in each target month according to the source rules,
uploads their cover images to Backblaze B2, and stores the records in Firestore.

Usage:
    python backfill_months.py [--year 2026] [--months 1,2,3,4,5] [--source all|vortexscans|thunderscans] [--dry-run]
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from typing import List

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from app.core.logging import get_logger
from app.services.b2_storage import b2_storage
from app.services.firebase import firebase_service
from app.services.series_index import series_index
from app.services.sources import SOURCES

logger = get_logger("backfill_months")


async def run_backfill(
    year: int = 2026,
    months: List[int] = None,
    source_choice: str = "all",
    dry_run: bool = False,
):
    if months is None:
        months = [1, 2, 3, 4, 5]

    print("=" * 65)
    print(f"HISTORICAL BACKFILL: Year {year} | Months {months}")
    print(f"Source: {source_choice} | Dry Run: {dry_run}")
    print("=" * 65)

    # 1. Verify B2 connection
    print("Verifying Backblaze B2 connection...")
    b2_check = await b2_storage.test_connection()
    if not b2_check.get("ok"):
        print(f"Error: Backblaze B2 check failed: {b2_check.get('error')}")
        sys.exit(1)
    print(f"Backblaze B2 OK: Bucket = {b2_check.get('bucket')} | Download URL = {b2_check.get('download_url')}")

    # 2. Verify Firebase
    print("Initializing Firebase...")
    firebase_service.initialize()
    if firebase_service.db is None:
        print("Error: Firebase Firestore not available")
        sys.exit(1)
    print("Firebase OK.")

    # 3. Determine sources
    sources_to_run = (
        [SOURCES[source_choice]]
        if source_choice in SOURCES
        else list(SOURCES.values())
    )

    overall_start = time.monotonic()
    total_new_saved = 0
    total_already_stored = 0

    for src in sources_to_run:
        print("\n" + "#" * 65)
        print(f"Source: {src.name} ({src.site})")
        print("#" * 65)

        for m in months:
            month_str = f"{year:04d}-{m:02d}"
            print(f"\n--- Scraping {src.name} for {month_str} ---")
            m_start = time.monotonic()

            try:
                result = await src.scraper.run_and_store(
                    year=year,
                    month=m,
                    dry_run=dry_run,
                )
                m_dur = round(time.monotonic() - m_start, 2)
                print(
                    f"[{month_str}] Finished in {m_dur}s: "
                    f"Saved={result.saved}, "
                    f"Already Stored={result.already_stored}, "
                    f"Matched Month={result.matched_month}, "
                    f"Candidates Seen={result.candidates_seen}"
                )
                if result.saved_slugs:
                    print(f"  New series saved: {len(result.saved_slugs)}")

                total_new_saved += result.saved
                total_already_stored += result.already_stored

            except Exception as e:
                logger.error(f"Failed to scrape {src.id} for {month_str}: {e}")
                print(f"[{month_str}] ERROR: {e}")

            # Invalidate memory cache so next query sees fresh data
            series_index.invalidate(src.collection)

    duration = round(time.monotonic() - overall_start, 2)
    print("\n" + "=" * 65)
    print(f"BACKFILL COMPLETE in {duration}s!")
    print(f"Total New Series Saved: {total_new_saved}")
    print(f"Total Skipped (Already Stored): {total_already_stored}")
    print("=" * 65)


def parse_months(s: str) -> List[int]:
    """Parse '1,2,3,4,5' or '1-5' into a list of integers."""
    if "-" in s and "," not in s:
        start, end = map(int, s.split("-"))
        return list(range(start, end + 1))
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Backfill comic data by month")
    parser.add_argument("--year", type=int, default=2026, help="Year to backfill (default: 2026)")
    parser.add_argument(
        "--months",
        type=parse_months,
        default=[1, 2, 3, 4, 5],
        help="Comma-separated or range of months, e.g. '1,2,3,4,5' or '1-5' (default: 1-5)",
    )
    parser.add_argument(
        "--source",
        choices=["all", "vortexscans", "thunderscans"],
        default="all",
        help="Source to backfill (default: all)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to DB or B2")

    args = parser.parse_args()
    asyncio.run(
        run_backfill(
            year=args.year,
            months=args.months,
            source_choice=args.source,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
