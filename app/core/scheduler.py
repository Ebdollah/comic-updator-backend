"""Background scheduler for recurring scrape jobs.

Runs in the same process as the API on an asyncio scheduler. That keeps the
setup simple; if the scrape ever grows heavy, move it to a separate worker
process so a long job cannot compete with request handling.
"""

from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

VORTEXSCANS_JOB_ID = "vortexscans_scrape"

_scheduler: Optional[AsyncIOScheduler] = None


async def _run_vortexscans_scrape() -> None:
    """The scheduled job: scrape every registered source, one after another.

    Sources run sequentially rather than concurrently so the two sites are
    never hit at the same time, and so one slow source cannot starve the
    other of connections.
    """
    from app.services.sources import SOURCES, run_source

    for source in SOURCES.values():
        logger.info(f"Scheduled scrape starting: {source.id}")
        try:
            result = await run_source(source)
            logger.info(
                f"Scheduled scrape finished for {source.id}: {result.saved} saved, "
                f"{result.already_stored} already stored, {result.duration_seconds}s"
            )
        except Exception as e:
            # One failing source must not stop the others, or kill the job.
            logger.error(f"Scheduled scrape failed for {source.id}: {e}")


def start_scheduler() -> Optional[AsyncIOScheduler]:
    """Start the scheduler if enabled. Safe to call once at startup."""
    global _scheduler

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled by configuration")
        return None

    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_vortexscans_scrape,
        # A fixed daily time rather than an interval: an interval restarts its
        # countdown on every app restart, so a frequently-restarted dev server
        # could go a long time without ever completing a run.
        trigger=CronTrigger(
            hour=settings.scrape_cron_hour,
            minute=settings.scrape_cron_minute,
            timezone="UTC",
        ),
        id=VORTEXSCANS_JOB_ID,
        name="Scrape every registered source for new series",
        max_instances=1,       # never overlap runs
        coalesce=True,         # if runs were missed, do one, not a backlog
        misfire_grace_time=3600,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(f"Scheduler started, daily at {schedule_description()}")
    return _scheduler


def schedule_description() -> str:
    """Human-readable schedule, e.g. 'Daily at 03:00 UTC'."""
    return f"Daily at {settings.scrape_cron_hour:02d}:{settings.scrape_cron_minute:02d} UTC"


def shutdown_scheduler() -> None:
    """Stop the scheduler on application shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def get_scheduler_status() -> dict:
    """Describe the scheduler for the status endpoint."""
    if _scheduler is None:
        return {"enabled": False, "running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        nxt = getattr(job, "next_run_time", None)
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run_at": nxt.astimezone(timezone.utc).isoformat() if nxt else None,
            }
        )

    return {
        "enabled": settings.scheduler_enabled,
        "running": _scheduler.running,
        "schedule": schedule_description(),
        "now": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
    }
