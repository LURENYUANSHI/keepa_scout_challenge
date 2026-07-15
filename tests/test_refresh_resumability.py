"""Resumability (HARNESS.md §8's kill/restart flow: "进行到一半 kill 再 up，
重新触发后...只处理剩余的") + per-item failure isolation + the atomic
done/failed counter driving the job's completion transition.

Same rationale as tests/test_refresh_reentrancy.py for not using the
`db_session` fixture: `_start_refresh`/`_refresh_one_asin_async` commit
internally, and these tests need those commits to actually land.
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asin import Asin
from app.models.refresh import RefreshJob, RefreshJobItem
from app.tasks.refresh_tasks import _record_item_result, _refresh_one_asin_async, _start_refresh

pytestmark = pytest.mark.asyncio


async def _reset_refresh_tables(test_engine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE refresh_job_items, refresh_jobs, asin_price_stats, asins "
                "RESTART IDENTITY CASCADE"
            )
        )


async def test_resuming_a_running_job_does_not_redo_done_items(test_engine):
    """Fixture directly simulates a job that was killed mid-run: `state`
    stuck at 'running' (the process died before it could mark itself
    done/failed), one item already `state='done'`, one still `pending`.

    Re-triggering must:
      - return the SAME job_id (no duplicate job)
      - NOT insert new RefreshJobItem rows
      - only (re-)enqueue the still-pending item
      - leave the already-done item's `updated_at` untouched
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    done_asin = f"TESTRESUME{uuid.uuid4().hex[:6].upper()}"
    pending_asin = f"TESTRESUME{uuid.uuid4().hex[:6].upper()}"
    job_id = f"r-{uuid.uuid4().hex[:12]}"

    async with session_maker() as session:
        session.add(Asin(asin=done_asin, supplier_cost=10.0))
        session.add(Asin(asin=pending_asin, supplier_cost=12.0))
        await session.commit()

        session.add(
            RefreshJob(
                job_id=job_id,
                state="running",
                trigger_source="manual",
                triggered_by=None,
                total=2,
                done=1,
                failed=0,
            )
        )
        session.add(RefreshJobItem(job_id=job_id, asin=done_asin, state="done"))
        session.add(RefreshJobItem(job_id=job_id, asin=pending_asin, state="pending"))
        await session.commit()

    async with session_maker() as session:
        done_item_before = await session.get(RefreshJobItem, {"job_id": job_id, "asin": done_asin})
        updated_at_before = done_item_before.updated_at

    with patch("app.tasks.refresh_tasks.refresh_one_asin.delay") as mock_delay:
        async with session_maker() as session:
            result = await _start_refresh(session, trigger_source="manual", triggered_by=None)

    assert result["job_id"] == job_id  # same job continued, not a new one
    assert result["state"] == "running"

    # Only the still-pending asin got (re-)enqueued -- the done one was
    # never touched.
    mock_delay.assert_called_once_with(job_id, pending_asin)

    async with session_maker() as session:
        items = (
            (await session.execute(select(RefreshJobItem).where(RefreshJobItem.job_id == job_id)))
            .scalars()
            .all()
        )
        assert len(items) == 2  # no new rows inserted for a resumed job

        done_item_after = await session.get(RefreshJobItem, {"job_id": job_id, "asin": done_asin})
        assert done_item_after.updated_at == updated_at_before  # not re-touched


async def test_per_item_failure_isolation_and_job_completion(test_engine):
    """One ASIN's fetch fails -- the rest of the batch must still complete,
    the failure must land in `failed` (not `done`), and once every item has
    resolved (`done + failed == total`) the job must flip to
    `state='done'`.
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    ok_asins = [f"TESTISO{uuid.uuid4().hex[:6].upper()}" for _ in range(2)]
    bad_asin = f"TESTISO{uuid.uuid4().hex[:6].upper()}"
    all_asins = [*ok_asins, bad_asin]

    async with session_maker() as session:
        for asin in all_asins:
            session.add(Asin(asin=asin, supplier_cost=10.0))
        await session.commit()

    async def _fake_fetch(session, keepa_client, asin, supplier_cost):
        if asin == bad_asin:
            return {"asin": asin, "status": "error", "error": "boom"}
        return {"asin": asin, "status": "ok", "error": None}

    with patch("app.tasks.refresh_tasks.get_keepa_client"), patch(
        "app.tasks.refresh_tasks.fetch_and_upsert_asin", side_effect=_fake_fetch
    ), patch("app.tasks.refresh_tasks.refresh_one_asin.delay"), patch(
        # _refresh_one_asin_async normally opens its session via
        # app.db.async_session_maker (production DB, correct for a real
        # Celery worker) -- redirect it to the test DB's session maker so
        # this test can drive the task body directly against test_engine.
        "app.tasks.refresh_tasks.async_session_maker",
        session_maker,
    ):
        async with session_maker() as session:
            job = await _start_refresh(session, trigger_source="manual", triggered_by=None)

        job_id = job["job_id"]

        # Drive the per-item task body directly (bypassing Celery/the
        # broker) -- HARNESS.md §8's "single ASIN fails, rest continue"
        # test only needs the task body's isolation behavior, not a real
        # broker round-trip.
        for asin in all_asins:
            await _refresh_one_asin_async(job_id, asin)

    async with session_maker() as session:
        refreshed_job = await session.get(RefreshJob, job_id)
        assert refreshed_job.done == len(ok_asins)
        assert refreshed_job.failed == 1
        assert refreshed_job.state == "done"  # done + failed == total
        assert refreshed_job.finished_at is not None

        items = (
            (await session.execute(select(RefreshJobItem).where(RefreshJobItem.job_id == job_id)))
            .scalars()
            .all()
        )
        states = {item.asin: item.state for item in items}
        for asin in ok_asins:
            assert states[asin] == "done"
        assert states[bad_asin] == "failed"


async def test_duplicate_completion_for_the_same_item_does_not_double_count(test_engine):
    """Regression test for a bug caught live by scripts/verify_refresh_resume.sh
    against a REAL worker (not a mocked `.delay()`): `_start_refresh` can
    legitimately enqueue a Celery task for the same still-`pending`
    (job_id, asin) more than once -- e.g. two back-to-back `POST /refresh`
    calls before either task has run yet (HARNESS.md §8's own required
    reentrancy check; test_refresh_reentrancy.py's
    `test_back_to_back_calls_return_same_job_and_no_duplicate_items` even
    asserts `.delay()` gets called twice for this exact scenario). Both
    enqueued copies eventually run and both call `_record_item_result` for
    the same item.

    Calling `_record_item_result` twice for the same (job_id, asin) must
    increment the job's `done`/`failed` counter only ONCE -- the second call
    is a no-op against an already-terminal item, not a double-count. Without
    this guard, `done + failed` can exceed `total` and the job can flip to
    `state='done'` from inflated counts while a different item is still
    genuinely `pending` forever.
    """
    await _reset_refresh_tables(test_engine)
    session_maker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    asin = f"TESTDUP{uuid.uuid4().hex[:6].upper()}"
    other_asin = f"TESTDUP{uuid.uuid4().hex[:6].upper()}"
    job_id = f"r-{uuid.uuid4().hex[:12]}"

    async with session_maker() as session:
        session.add(Asin(asin=asin, supplier_cost=10.0))
        session.add(Asin(asin=other_asin, supplier_cost=10.0))
        await session.commit()

        session.add(
            RefreshJob(
                job_id=job_id, state="running", trigger_source="manual",
                triggered_by=None, total=2, done=0, failed=0,
            )
        )
        session.add(RefreshJobItem(job_id=job_id, asin=asin, state="pending"))
        session.add(RefreshJobItem(job_id=job_id, asin=other_asin, state="pending"))
        await session.commit()

    # Two independent "task runs" resolve the SAME asin as done -- simulates
    # two duplicate-enqueued Celery tasks for the same still-pending item
    # both executing.
    async with session_maker() as session:
        await _record_item_result(session, job_id, asin, "ok")
    async with session_maker() as session:
        await _record_item_result(session, job_id, asin, "ok")  # duplicate

    async with session_maker() as session:
        job = await session.get(RefreshJob, job_id)
        assert job.done == 1, f"expected done=1 (not double-counted), got {job.done}"
        assert job.failed == 0
        assert job.state == "running"  # other_asin is still genuinely pending

        item = await session.get(RefreshJobItem, {"job_id": job_id, "asin": asin})
        assert item.state == "done"

    # Resolving the genuinely-pending second item completes the job normally.
    async with session_maker() as session:
        await _record_item_result(session, job_id, other_asin, "error")

    async with session_maker() as session:
        job = await session.get(RefreshJob, job_id)
        assert job.done == 1
        assert job.failed == 1
        assert job.done + job.failed == job.total == 2
        assert job.state == "done"
