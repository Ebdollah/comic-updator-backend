from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.core.scheduler import get_scheduler_status
from app.models.vortexscans import ScrapeResult, VortexSeries
from app.services.firebase import firebase_service
from app.services.sources import get_source, list_sources

logger = get_logger(__name__)
router = APIRouter()

SourcePath = Path(..., description="Source id, e.g. 'vortexscans' or 'thunderscans'")


def _resolve(source: str):
    try:
        return get_source(source)
    except KeyError:
        known = ", ".join(s["id"] for s in list_sources())
        raise HTTPException(404, f"Unknown source '{source}'. Available: {known}")


@router.get("")
async def get_sources() -> List[dict]:
    """List the sources this backend can scrape. Drives the dashboard tabs."""
    return list_sources()


@router.post("/{source}/scrape", response_model=ScrapeResult)
async def scrape_source(
    source: str = SourcePath,
    year: Optional[int] = Query(None, description="Defaults to the current UTC year"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Defaults to now"),
    dry_run: bool = Query(False, description="Parse and filter, but write nothing"),
) -> ScrapeResult:
    """Scrape one source now. Series already stored are skipped, never rewritten."""
    src = _resolve(source)
    try:
        return await src.scraper.run_and_store(year=year, month=month, dry_run=dry_run)
    except Exception as e:
        logger.error(f"Scrape failed for {source}: {e}")
        raise HTTPException(502, f"Scrape failed: {e}")


@router.get("/{source}/series", response_model=List[VortexSeries])
async def list_source_series(
    source: str = SourcePath,
    limit: int = Query(200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Everything stored for one source."""
    src = _resolve(source)
    try:
        return await run_in_threadpool(firebase_service.list_documents, src.collection, limit)
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")


@router.get("/{source}/series/{slug:path}", response_model=VortexSeries)
async def get_source_series(source: str, slug: str) -> Dict[str, Any]:
    """One stored series. Slugs may contain apostrophes, hence the path converter."""
    src = _resolve(source)
    try:
        doc = await run_in_threadpool(firebase_service.get_document, src.collection, slug)
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")
    if doc is None:
        raise HTTPException(404, f"Series '{slug}' not found in {source}")
    return doc


@router.get("/{source}/status")
async def source_status(source: str = SourcePath) -> Dict[str, Any]:
    """Stored count, scheduler state and the last run for one source."""
    src = _resolve(source)
    try:
        stored = len(await run_in_threadpool(firebase_service.existing_ids, src.collection))
        db_ok = True
    except Exception as e:
        logger.error(f"Status: database unreachable: {e}")
        stored, db_ok = 0, False

    last = getattr(src.scraper, "last_result", None)
    last_at = getattr(src.scraper, "last_run_at", None)
    return {
        "source": src.id,
        "name": src.name,
        "site": src.site,
        "notes": src.notes,
        "database_connected": db_ok,
        "stored_series": stored,
        "scheduler": get_scheduler_status(),
        "last_run_at": last_at.isoformat() if last_at else None,
        "last_result": last.model_dump(mode="json") if last else None,
    }
