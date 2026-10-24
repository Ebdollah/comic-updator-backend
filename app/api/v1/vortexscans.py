from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.core.scheduler import get_scheduler_status
from app.models.vortexscans import ScrapeResult, VortexSeries
from app.services.firebase import firebase_service
from app.services.series_index import series_index
from app.services.vortexscans import COLLECTION, vortexscans_scraper

logger = get_logger(__name__)
router = APIRouter()


@router.post("/scrape", response_model=ScrapeResult)
async def scrape_vortexscans(
    year: Optional[int] = Query(None, description="Target year, defaults to the current UTC year"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Target month, defaults to now"),
    dry_run: bool = Query(False, description="Parse and filter, but write nothing to Firestore"),
) -> ScrapeResult:
    """Scrape Latest Releases and store series whose chapter 1 dropped this month.

    Series already in Firestore are skipped, so this is safe to run repeatedly.
    """
    try:
        result = await vortexscans_scraper.run_and_store(year=year, month=month, dry_run=dry_run)
        if not dry_run:
            series_index.invalidate(COLLECTION)
        return result
    except Exception as e:
        logger.error(f"Scrape failed: {e}")
        raise HTTPException(status_code=502, detail=f"Scrape failed: {e}")


@router.get("/series", response_model=List[VortexSeries])
async def list_vortexscans_series(
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List the series stored from vortexscans.org."""
    try:
        return await run_in_threadpool(firebase_service.list_documents, COLLECTION, limit)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")


@router.get("/series/{slug}", response_model=VortexSeries)
async def get_vortexscans_series(slug: str) -> Dict[str, Any]:
    """Fetch one stored series by its slug."""
    try:
        doc = await run_in_threadpool(firebase_service.get_document, COLLECTION, slug)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Series '{slug}' not found")
    return doc


@router.get("/status")
async def vortexscans_status() -> Dict[str, Any]:
    """Scheduler state, last run, and how many series are stored.

    Powers the status bar in the dashboard.
    """
    try:
        stored = len(await run_in_threadpool(firebase_service.existing_ids, COLLECTION))
        db_ok = True
    except Exception as e:
        logger.error(f"Status: database unreachable: {e}")
        stored, db_ok = 0, False

    last = vortexscans_scraper.last_result
    return {
        "database_connected": db_ok,
        "stored_series": stored,
        "scheduler": get_scheduler_status(),
        "last_run_at": (
            vortexscans_scraper.last_run_at.isoformat()
            if vortexscans_scraper.last_run_at
            else None
        ),
        "last_result": last.model_dump(mode="json") if last else None,
    }


@router.get("/preview", response_model=ScrapeResult)
async def preview_vortexscans(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None, ge=1, le=12),
) -> ScrapeResult:
    """See what a scrape would pick up, without touching the database."""
    result, new_series = await vortexscans_scraper.scrape_new_series(year=year, month=month)
    result.dry_run = True
    result.saved_slugs = [s.slug for s in new_series]
    return result
