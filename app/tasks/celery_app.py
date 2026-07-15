"""Celery app instance — see ARCHITECTURE.md §1 / §5 / §6.

`docker-compose.yml`'s `worker` and `beat` services both run
`celery -A app.tasks.celery_app <worker|beat>`, so this module is the single
place broker config + the beat schedule live.

Result backend: deliberately NOT configured. ARCHITECTURE.md §1 is explicit
that "断点续跑的真相源在 Postgres 的 refresh_job_items,不在 Redis" — task
state truth lives in `refresh_jobs`/`refresh_job_items`, not in Celery's own
result backend, so nothing here needs `AsyncResult` polling. Tasks are fired
with `.delay()`/`.apply_async()` and never awaited for a return value.

Beat schedule: one cron entry, daily at 04:00 UTC (ARCHITECTURE.md §6),
calling `run_scheduled_refresh` — a thin sync wrapper (defined in
`refresh_tasks.py`) around the exact same `_start_refresh()` internal
function `POST /refresh` calls, so the reentrancy/resumability guard is
shared, not reimplemented for the scheduled path (ARCHITECTURE.md §3.4).
"""
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery("keepa_scout", broker=settings.REDIS_URL)

# Explicit UTC — the containers also run with TZ=UTC (docker-compose.yml),
# but we don't want the beat schedule's meaning to depend on that env var
# being set correctly; crontab(hour=4, minute=0) below is unambiguous only
# if the Celery-level timezone is pinned too.
celery_app.conf.timezone = "UTC"
celery_app.conf.enable_utc = True

celery_app.conf.task_ignore_result = True

celery_app.conf.beat_schedule = {
    "daily-refresh-04-00-utc": {
        "task": "app.tasks.refresh_tasks.run_scheduled_refresh",
        "schedule": crontab(hour=4, minute=0),
    },
}

# Import task modules so their `@celery_app.task` decorators register with
# this app instance. Done at the bottom (not top) of the file to avoid a
# circular import: refresh_tasks.py does `from app.tasks.celery_app import
# celery_app`, which requires `celery_app` to already exist in this module's
# namespace by the time that import executes.
import app.tasks.refresh_tasks  # noqa: E402,F401
