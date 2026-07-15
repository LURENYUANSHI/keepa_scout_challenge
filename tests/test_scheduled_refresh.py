"""HARNESS.md §8's scheduled-refresh coverage that isn't already exercised by
tests/test_refresh_reentrancy.py's `trigger_source`/`triggered_by` field
checks:

  1. `celery_app.beat_schedule` has a cron entry firing daily at 04:00 UTC,
     pointing at the scheduled-refresh task (which wraps the same
     `_start_refresh()` internal function `POST /refresh` uses -- see
     app/tasks/refresh_tasks.py's `run_scheduled_refresh`).
  2. Calling `_start_refresh(trigger_source="scheduled", triggered_by=None)`
     while a job is already `state='running'` is a pure no-op with respect
     to job creation -- it does NOT create a second job. Doesn't wait for a
     real 04:00 UTC trigger -- drives `_start_refresh` directly, exactly as
     HARNESS.md §8's evidence block describes.

Does NOT use the `db_session` fixture -- same rationale as
tests/test_refresh_reentrancy.py: `_start_refresh` commits internally, and
this test needs those commits to actually land in a fresh session.
"""
import uuid

from unittest.mock import patch

import pytest
from celery.schedules import crontab
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asin import Asin
from app.models.refresh import RefreshJob, RefreshJobItem
from app.tasks.celery_app import celery_app
from app.tasks.refresh_tasks import _start_refresh

pytestmark = pytest.mark.asyncio


async def _reset_refresh_tables(test_engine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE refresh_job_items, refresh_jobs, asin_price_stats, asins "
                "RESTART IDENTITY CASCADE"
            )
        )


# --- beat schedule config --------------------------------------------------


def test_beat_schedule_has_a_daily_04_00_utc_cron_entry():
    """`python -c "from app.tasks.celery_app import app; print(app.conf.beat_schedule)"`
    (HARNESS.md §8's evidence) must show a cron entry at 04:00 UTC pointing
    at the scheduled-refresh task."""
    schedule = celery_app.conf.beat_schedule
    assert schedule, "celery_app.conf.beat_schedule is empty -- no scheduled refresh configured"

    entries = list(schedule.values())
    matching = [
        entry
        for entry in entries
        if entry.get("task") == "app.tasks.refresh_tasks.run_scheduled_refresh"
    ]
    assert matching, (
        f"no beat_schedule entry targets app.tasks.refresh_tasks.run_scheduled_refresh; "
        f"got tasks={[e.get('task') for e in entries]}"
    )

    entry = matching[0]
    schedule_obj = entry["schedule"]
    assert isinstance(schedule_obj, crontab), (
        f"expected a crontab schedule, got {type(schedule_obj)!r}"
    )
    # crontab() normalizes hour/minute into internal sets -- 4 and 0 land in
    # `hour`/`minute` respectively regardless of how the entry was
    # constructed (crontab(hour=4, minute=0) vs a "0 4 * * *" string).
    assert set(schedule_obj.hour) == {4}, f"expected hour=4 (UTC), got {schedule_obj.hour}"
    assert set(schedule_obj.minute) == {0}, f"expected minute=0, got {schedule_obj.minute}"

    # ARCHITECTURE.md §6 / celery_app.py: the whole point of a fixed UTC
    # schedule is that it doesn't depend on the process's local timezone
    # interpretation -- confirm that's actually configured, not just assumed.
    assert celery_app.conf.enable_utc is True
    assert celery_app.conf.timezone == "UTC"


def test_run_scheduled_refresh_task_wraps_start_refresh_with_scheduled_trigger():
    """`run_scheduled_refresh` (the beat task body) must be the thin wrapper
    documented in app/tasks/refresh_tasks.py -- calling `_start_refresh` with
    `trigger_source="scheduled", triggered_by=None`, not a separate
    reimplementation of the reentrancy/resumability logic."""
    import inspect

    from app.tasks import refresh_tasks

    source = inspect.getsource(refresh_tasks._run_scheduled_refresh_async)
    assert "_start_refresh" in source
    assert 'trigger_source="scheduled"' in source
    assert "triggered_by=None" in source


# --- scheduled trigger, no-op when a job is already running ----------------


async def test_scheduled_trigger_is_a_pure_noop_when_a_job_is_already_running(test_engine):
    """A daily 04:00 UTC beat firing while a manually-triggered refresh is
    still in progress must NOT create a second job -- HARNESS.md §8:
    "定时触发命中'已有任务在跑'分支时是纯粹的空操作，不产生第二个 job". This
    drives `_start_refresh(trigger_source='scheduled', ...)` directly against
    a fixture where a job is already `state='running'`, rather than waiting
    for a real cron tick.
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    asin = f"TESTSCHED{uuid.uuid4().hex[:6].upper()}"
    running_job_id = f"r-{uuid.uuid4().hex[:12]}"

    async with session_maker() as session:
        session.add(Asin(asin=asin, supplier_cost=10.0))
        await session.commit()

        session.add(
            RefreshJob(
                job_id=running_job_id,
                state="running",
                trigger_source="manual",
                triggered_by=None,
                total=1,
                done=0,
                failed=0,
            )
        )
        session.add(RefreshJobItem(job_id=running_job_id, asin=asin, state="pending"))
        await session.commit()

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay") as mock_delay:
        async with session_maker() as session:
            result = await _start_refresh(session, trigger_source="scheduled", triggered_by=None)

    # Folds into the existing running job -- same job_id, no new row.
    assert result["job_id"] == running_job_id
    assert result["state"] == "running"
    mock_delay.assert_called_once_with(running_job_id, asin)

    async with session_maker() as session:
        jobs = (await session.execute(select(RefreshJob))).scalars().all()
        assert len(jobs) == 1  # still exactly one job row -- no second job created
        assert jobs[0].job_id == running_job_id
        assert jobs[0].trigger_source == "manual"  # untouched -- the ORIGINAL job's fields, not overwritten


async def test_scheduled_trigger_starts_a_fresh_job_when_none_is_running(test_engine):
    """The complement of the no-op case above: when nothing is currently
    running, a scheduled trigger DOES start a real job, correctly stamped
    `trigger_source='scheduled'` and `triggered_by IS NULL` (beat never has a
    user_id -- ARCHITECTURE.md §3.4)."""
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    asin = f"TESTSCHED{uuid.uuid4().hex[:6].upper()}"
    async with session_maker() as session:
        session.add(Asin(asin=asin, supplier_cost=10.0))
        await session.commit()

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay") as mock_delay:
        async with session_maker() as session:
            result = await _start_refresh(session, trigger_source="scheduled", triggered_by=None)

    mock_delay.assert_called_once_with(result["job_id"], asin)

    async with session_maker() as session:
        job = await session.get(RefreshJob, result["job_id"])
        assert job.trigger_source == "scheduled"
        assert job.triggered_by is None
        assert job.state == "running"
