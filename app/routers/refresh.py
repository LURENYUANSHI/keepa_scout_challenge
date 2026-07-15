"""POST /refresh, GET /refresh/status — see ARCHITECTURE.md §3.4.

Both endpoints are intentionally thin: `app/tasks/refresh_tasks.py` owns
every bit of reentrancy/resumability logic (shared with Celery beat's
scheduled trigger), and the per-ASIN Celery task body. This router just does
auth + a read.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db import get_db
from app.models.refresh import RefreshJob
from app.models.user import User
from app.tasks.refresh_tasks import _start_refresh

router = APIRouter(tags=["refresh"])


@router.post("/refresh")
async def start_refresh(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Kick off (or resume/no-op into) a full refresh. Returns immediately
    -- HARNESS.md §8 wants this back in well under a second, it never waits
    on the Celery tasks it enqueues to finish. Shape matches CHALLENGE.md's
    `/refresh` example: `{"job_id", "state", "total", "done"}`.
    """
    return await _start_refresh(db, trigger_source="manual", triggered_by=str(user.id))


@router.get("/refresh/status")
async def refresh_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Most recent refresh job's current state. If no job has ever run,
    returns a null-ish placeholder rather than 404/crashing -- there's
    nothing wrong with the system, it just hasn't refreshed yet.

    `last_refresh_at`: `finished_at` once the job has wrapped up, otherwise
    `started_at` -- i.e. "when did the most recent refresh activity happen,"
    which for a still-running job is when it started and for a finished one
    is when it finished.
    """
    result = await db.execute(
        select(RefreshJob).order_by(RefreshJob.started_at.desc()).limit(1)
    )
    job = result.scalars().first()

    if job is None:
        return {
            "job_id": None,
            "state": None,
            "total": 0,
            "done": 0,
            "failed": 0,
            "last_refresh_at": None,
        }

    last_refresh_at = job.finished_at if job.finished_at is not None else job.started_at

    return {
        "job_id": job.job_id,
        "state": job.state,
        "total": job.total,
        "done": job.done,
        "failed": job.failed,
        "last_refresh_at": last_refresh_at,
    }
