"""`_start_refresh()` (shared by `POST /refresh` and Celery beat) + the
per-ASIN Celery task that actually calls Keepa. See ARCHITECTURE.md §3.4 and
HARNESS.md §8 for the exact contract this implements.

Reentrancy + resumability -- read this before touching `_start_refresh`
------------------------------------------------------------------------
A literal reading of ARCHITECTURE.md §3.4's sequence diagram ("already have
a running job -> just return its job_id, do nothing else") is *correct* for
the ordinary concurrent-trigger case (two people hit `POST /refresh` within
the same second) but *insufficient* for the kill/restart case HARNESS.md §8
actually tests: `docker compose kill` on the worker leaves the DB row
sitting at `state='running'` forever -- nothing crashes the DB write, the
*process* just dies mid-batch, so there is no live Celery task actually
working the still-pending items anymore. If re-triggering after that only
returned the existing `job_id` and enqueued nothing, the job would be
permanently stuck at `state='running'` with no forward progress -- which
directly contradicts HARNESS.md §8's "重新触发后...只处理剩余的" flow.

So `_start_refresh` does this instead: if a `state='running'` job exists,
re-enqueue Celery tasks for whichever of its `RefreshJobItem` rows are still
`state='pending'` (never for ones already `done`/`failed` -- that's the
"already-completed ASINs are not re-fetched" guarantee, and it's also why
`updated_at` on a `done` item doesn't move across a resume). This is safe to
call repeatedly / from concurrent callers because:
  - `refresh_one_asin` only ever touches `RefreshJobItem` rows in `pending`
    state's ASIN (see the query above) -- an item that's already `done`
    never gets re-enqueued, so its `updated_at` never moves.
  - Enqueuing the *same still-pending* item twice before either enqueued
    task has run is a harmless duplicate: both invocations upsert the same
    Keepa data and flip the same item row from `pending` -> `done`/`failed`;
    the atomic counter UPDATE in `_record_item_result` (see below) makes
    double-counting on that race the only thing worth guarding against, and
    it does.
No new `RefreshJobItem` rows are ever inserted for a resumed job -- only for
a brand-new one -- so resuming never duplicates the item set.

A Postgres advisory lock (`pg_advisory_xact_lock`) serializes the
check-for-a-running-job / create-a-new-job sequence across concurrent
callers (two truly simultaneous `POST /refresh` calls in different DB
transactions), closing the race a plain SELECT-then-INSERT would have.
"""
import asyncio
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker, engine
from app.ingest import fetch_and_upsert_asin
from app.keepa.client import get_keepa_client
from app.models.asin import Asin
from app.models.refresh import RefreshJob, RefreshJobItem
from app.tasks.celery_app import celery_app

# Fixed, arbitrary key for the advisory lock guarding "is a job already
# running / create a new one" -- any int works, it just needs to be the same
# constant everywhere this module takes the lock.
_REFRESH_START_LOCK_KEY = 727_100


async def _start_refresh(
    session: AsyncSession,
    trigger_source: str,
    triggered_by: str | None,
) -> dict[str, Any]:
    """Shared internal function behind `POST /refresh` and beat's daily
    scheduled trigger -- see this module's docstring and ARCHITECTURE.md
    §3.4. Never blocks on the refresh actually finishing; only enqueues
    Celery work and returns.
    """
    # Serialize concurrent callers across this whole check+create sequence.
    # Held for the duration of the current transaction (released on commit,
    # which `session.commit()` below triggers).
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REFRESH_START_LOCK_KEY})

    result = await session.execute(
        select(RefreshJob)
        .where(RefreshJob.state == "running")
        .order_by(RefreshJob.started_at.desc())
        .limit(1)
    )
    running_job = result.scalars().first()

    if running_job is not None:
        pending_result = await session.execute(
            select(RefreshJobItem.asin).where(
                RefreshJobItem.job_id == running_job.job_id,
                RefreshJobItem.state == "pending",
            )
        )
        pending_asins = [row[0] for row in pending_result.all()]

        # Release the advisory lock (transaction end) before enqueuing --
        # nothing below needs it, and there's nothing else to write.
        await session.commit()

        for asin in pending_asins:
            refresh_one_asin.delay(running_job.job_id, asin)

        return {
            "job_id": running_job.job_id,
            "state": running_job.state,
            "total": running_job.total,
            "done": running_job.done,
        }

    # No job currently running -> start a fresh one over every ASIN in the
    # catalog, carrying forward each ASIN's existing `supplier_cost` --
    # that's ours (from the original CSV), not something Keepa provides, so
    # a refresh must preserve it rather than wiping it to NULL.
    asin_rows = (await session.execute(select(Asin.asin, Asin.supplier_cost))).all()
    asin_costs: dict[str, float | None] = {
        row.asin: (float(row.supplier_cost) if row.supplier_cost is not None else None)
        for row in asin_rows
    }

    job_id = f"r-{uuid.uuid4().hex[:12]}"
    job = RefreshJob(
        job_id=job_id,
        state="running",
        trigger_source=trigger_source,
        triggered_by=uuid.UUID(triggered_by) if triggered_by else None,
        total=len(asin_costs),
        done=0,
        failed=0,
    )
    session.add(job)
    for asin in asin_costs:
        session.add(RefreshJobItem(job_id=job_id, asin=asin, state="pending"))

    await session.commit()

    for asin in asin_costs:
        refresh_one_asin.delay(job_id, asin)

    return {"job_id": job_id, "state": "running", "total": len(asin_costs), "done": 0}


async def _record_item_result(session: AsyncSession, job_id: str, asin: str, status: str) -> None:
    """Mark one `RefreshJobItem` done/failed and atomically bump the parent
    `RefreshJob`'s counter, flipping the job to `state='done'` the moment
    `done + failed == total`.

    `status="not_found"` counts as `failed` here -- CHALLENGE.md's
    `/refresh/status` shape only has a `failed` counter, no third bucket.

    The counter bump uses a single atomic `UPDATE ... RETURNING` (not a
    read-modify-write in Python) specifically so concurrent workers
    finishing different items at the same moment can't stomp on each
    other's increment or race past the "all done" transition -- the
    `WHERE state='running'` guard on the completion UPDATE means at most one
    concurrent finisher actually flips the job to `done`, the rest are
    no-ops against a row that's already there.

    Idempotency guard (found via scripts/verify_refresh_resume.sh against a
    REAL worker, not a mocked-out unit test): `_start_refresh` can legitimately
    enqueue a Celery task for the SAME still-`pending` (job_id, asin) more
    than once -- e.g. two back-to-back `POST /refresh` calls before either
    task has run yet (HARNESS.md §8's own required reentrancy check: "连续
    两次 POST /refresh，断言两次返回同一个 job_id"; tests/test_refresh_reentrancy.py
    even asserts `.delay()` gets called twice for this exact scenario). Both
    enqueued copies eventually execute and both call this function for the
    same item. The item-state `UPDATE` below is therefore conditioned on
    `state == 'pending'`: only the copy that actually flips
    pending -> done/failed increments the `done`/`failed` counter; a later
    duplicate finding the item already terminal is a pure no-op. Without
    this guard the counters double-count, `done + failed` can exceed
    `total`, and (worse) the job can flip to `state='done'` from inflated
    counts while a genuinely different item is still stuck `pending`
    forever with nothing left to re-enqueue it.
    """
    item_state = "done" if status == "ok" else "failed"
    counter_col = "done" if item_state == "done" else "failed"

    update_result = await session.execute(
        update(RefreshJobItem)
        .where(
            RefreshJobItem.job_id == job_id,
            RefreshJobItem.asin == asin,
            RefreshJobItem.state == "pending",
        )
        .values(state=item_state)
    )

    if update_result.rowcount == 0:
        # Already resolved by a prior/duplicate task run for this same
        # (job_id, asin) -- see the idempotency note above. Nothing left to
        # do: don't double-count into done/failed, don't re-evaluate the
        # completion transition (whichever call actually resolved this item
        # already did that check).
        await session.commit()
        return

    # counter_col is one of the two fixed literals above, never user input.
    result = await session.execute(
        text(
            f"UPDATE refresh_jobs SET {counter_col} = {counter_col} + 1 "
            "WHERE job_id = :job_id RETURNING done, failed, total"
        ),
        {"job_id": job_id},
    )
    row = result.one()

    if row.done + row.failed >= row.total:
        await session.execute(
            update(RefreshJob)
            .where(RefreshJob.job_id == job_id, RefreshJob.state == "running")
            .values(state="done", finished_at=func.now())
        )

    await session.commit()


async def _refresh_one_asin_async(job_id: str, asin: str) -> None:
    async with async_session_maker() as session:
        asin_row = await session.get(Asin, asin)
        supplier_cost = (
            float(asin_row.supplier_cost)
            if asin_row is not None and asin_row.supplier_cost is not None
            else None
        )

        keepa_client = get_keepa_client()
        result = await fetch_and_upsert_asin(session, keepa_client, asin, supplier_cost)

        await _record_item_result(session, job_id, asin, result["status"])


async def _run_and_dispose_engine(coro: Any) -> Any:
    """Await `coro`, then dispose `app.db.engine`'s connection pool before
    the current event loop closes.

    Discovered live while verifying this phase (see REPORT.md): Celery's
    prefork worker runs tasks sequentially inside one long-lived child
    process, each wrapped in its own `asyncio.run()` call -- a *fresh event
    loop per task*. asyncpg connections are bound to the event loop that
    created them, but `app.db.engine`'s connection pool is a *module-level
    singleton* shared across every task this child process ever runs. Task
    N pools a connection under loop N; without disposing, task N+1 (a new
    loop) can be handed that same pooled connection and asyncpg rejects it
    with `InterfaceError: cannot perform operation: another operation is in
    progress`. Disposing the pool here -- while still inside the loop that
    used it -- forces the next task's `asyncio.run()` to open fresh
    connections under its own loop instead of reusing stale ones.
    """
    try:
        return await coro
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.refresh_tasks.refresh_one_asin")
def refresh_one_asin(job_id: str, asin: str) -> None:
    """Per-ASIN Celery task: fetch+upsert one ASIN via the shared
    `app.ingest.fetch_and_upsert_asin` logic, then update this job's
    progress. Opens its own DB session -- Celery tasks run in the worker
    process and must not reuse a request-scoped session.

    Wrapped in `asyncio.run` because Celery invokes tasks synchronously but
    everything this task needs to do (DB I/O via async SQLAlchemy, the
    Keepa HTTP call) is async -- same bridging pattern `run_scheduled_refresh`
    below uses for beat's sync entrypoint, kept consistent across this
    module's two Celery tasks.
    """
    asyncio.run(_run_and_dispose_engine(_refresh_one_asin_async(job_id, asin)))


async def _run_scheduled_refresh_async() -> dict[str, Any]:
    async with async_session_maker() as session:
        return await _start_refresh(session, trigger_source="scheduled", triggered_by=None)


@celery_app.task(name="app.tasks.refresh_tasks.run_scheduled_refresh")
def run_scheduled_refresh() -> dict[str, Any]:
    """Celery beat's daily 04:00 UTC entrypoint (see `celery_app.py`'s
    `beat_schedule`). Bridges beat's sync call into `_start_refresh` with
    its own async session -- beat doesn't go through HTTP/auth at all
    (ARCHITECTURE.md §3.4), it calls the same internal function directly.
    """
    return asyncio.run(_run_and_dispose_engine(_run_scheduled_refresh_async()))
