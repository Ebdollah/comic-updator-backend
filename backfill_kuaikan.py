"""Backfill Kuaikan (Manhua tab): every Chinese series whose chapter 1 came out in a year.

The daily run only sees the site's "newly added" list (about three months
back), so earlier debuts need this one-off walk of the whole catalogue. The
same filters apply as in the daily run - excluded themes and BL/GL - and each
saved series gets its English title and a B2 cover. Safe to re-run: stored
series are skipped.

Usage:
    python backfill_kuaikan.py [--year 2026] [--dry-run]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from app.services.firebase import firebase_service
from app.services.kuaikan import SITE_TZ, kuaikan_scraper


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--year", type=int, default=datetime.now(SITE_TZ).year)
    parser.add_argument("--dry-run", action="store_true", help="Filter and build, but save nothing")
    args = parser.parse_args()

    firebase_service.initialize()
    results = await kuaikan_scraper.backfill_year(args.year, dry_run=args.dry_run)

    print(f"\nKuaikan backfill {args.year}{' (dry run)' if args.dry_run else ''}")
    print(f"{'month':<8} {'debuted':>8} {'stored':>7} {'saved':>6} {'theme':>6} {'BL/GL':>6}")
    totals = [0, 0, 0, 0, 0]
    for month, (result, skipped) in results.items():
        saved = len(result.saved_slugs) if args.dry_run else result.saved
        row = [
            result.matched_month,
            result.already_stored,
            saved,
            skipped.get("excluded theme", 0),
            skipped.get("BL/GL", 0),
        ]
        totals = [a + b for a, b in zip(totals, row)]
        print(f"{month:<8} {row[0]:>8} {row[1]:>7} {row[2]:>6} {row[3]:>6} {row[4]:>6}")
    print(f"{'total':<8} {totals[0]:>8} {totals[1]:>7} {totals[2]:>6} {totals[3]:>6} {totals[4]:>6}")


if __name__ == "__main__":
    asyncio.run(main())
