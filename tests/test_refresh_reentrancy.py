"""Reentrancy (HARNESS.md §8: "刷新还在跑时再次 POST /refresh，不能启动第二
个任务") + `trigger_source`/`triggered_by` correctness.

Does NOT use conftest.py's `db_session` fixture -- see
tests/test_etl_dirty_data.py's docstring for why: `_start_refresh` commits
internally, and these tests need those commits to actually land so a
second call (in a fresh session) sees them. Uses `test_engine` directly,
resetting the refresh-related tables at the start of each test instead of
relying on rollback.
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asin import Asin
from app.models.refresh import RefreshJob, RefreshJobItem
from app.models.user import User
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


async def test_back_to_back_calls_return_same_job_and_no_duplicate_items(test_engine):
    """Simulates two concurrent manual `POST /refresh` triggers landing
    back-to-back with no completed work yet: both must resolve to the SAME
    `job_id`, and only one set of `RefreshJobItem` rows must exist.
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    asins = [f"TESTREENT{uuid.uuid4().hex[:6].upper()}" for _ in range(3)]
    async with session_maker() as session:
        for asin in asins:
            session.add(Asin(asin=asin, supplier_cost=10.0))
        await session.commit()

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay") as mock_delay:
        async with session_maker() as session:
            first = await _start_refresh(session, trigger_source="manual", triggered_by=None)
        async with session_maker() as session:
            second = await _start_refresh(session, trigger_source="manual", triggered_by=None)

    assert first["job_id"] == second["job_id"]
    assert first["state"] == "running"
    assert first["total"] == len(asins)
    # First call creates the job and enqueues all 3 pending items; the
    # second call hits the "already running" branch and re-enqueues the
    # (still all-pending, nothing has actually run yet) 3 items again.
    assert mock_delay.call_count == len(asins) * 2

    async with session_maker() as session:
        jobs = (await session.execute(select(RefreshJob))).scalars().all()
        assert len(jobs) == 1  # no second job created

        items = (
            (
                await session.execute(
                    select(RefreshJobItem).where(RefreshJobItem.job_id == first["job_id"])
                )
            )
            .scalars()
            .all()
        )
        assert len(items) == len(asins)  # not duplicated
        assert {item.asin for item in items} == set(asins)


async def test_manual_trigger_records_user_scheduled_trigger_records_null(test_engine):
    """Manual calls must stamp `trigger_source='manual'` and
    `triggered_by=<user id>`; scheduled (beat) calls must stamp
    `trigger_source='scheduled'` and `triggered_by IS NULL`.
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    user_id = uuid.uuid4()
    asin = f"TESTTRIG{uuid.uuid4().hex[:6].upper()}"

    async with session_maker() as session:
        session.add(
            User(id=user_id, email=f"refresh-trig-{uuid.uuid4().hex}@example.com", password_hash="x")
        )
        session.add(Asin(asin=asin, supplier_cost=5.0))
        await session.commit()

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay"):
        async with session_maker() as session:
            manual_result = await _start_refresh(
                session, trigger_source="manual", triggered_by=str(user_id)
            )

    async with session_maker() as session:
        manual_job = await session.get(RefreshJob, manual_result["job_id"])
        assert manual_job.trigger_source == "manual"
        assert manual_job.triggered_by == user_id

        # Mark it done so the next call doesn't just hit the
        # already-running branch -- this test is about a *new* job's
        # fields, not the reentrancy guard (covered above).
        manual_job.state = "done"
        await session.commit()

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay"):
        async with session_maker() as session:
            scheduled_result = await _start_refresh(
                session, trigger_source="scheduled", triggered_by=None
            )

    async with session_maker() as session:
        scheduled_job = await session.get(RefreshJob, scheduled_result["job_id"])
        assert scheduled_job.trigger_source == "scheduled"
        assert scheduled_job.triggered_by is None
        assert scheduled_job.job_id != manual_result["job_id"]
