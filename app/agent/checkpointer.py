"""`AsyncPostgresSaver` initialization — LangGraph's short-term memory.

ARCHITECTURE.md §2: the checkpointer is what actually persists
`thread_id`-scoped conversation state (messages, tool_calls/ToolMessages,
and our custom `active_filters`/`last_result_asins`/`resolved_entity`
fields) across turns and across `docker compose restart` -- `chat_sessions`
itself only tracks ownership, not content (see app/models/chat.py).

Version note (verified against the pinned `langgraph-checkpoint-postgres`
in this repo's requirements.txt, not guessed from memory/newer docs):
`AsyncPostgresSaver.from_conn_string()` is an `@asynccontextmanager` wrapping
a single `psycopg.AsyncConnection` (not a pool) -- see this module's
`checkpointer_lifespan()` docstring for how that's threaded into
app/main.py's FastAPI lifespan. It speaks the `psycopg` (v3) wire protocol,
which wants a plain `postgresql://` DSN -- NOT SQLAlchemy's
`postgresql+asyncpg://` driver-qualified URL used elsewhere in this repo
(app/db.py). `to_psycopg_dsn()` below does that translation.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def to_psycopg_dsn(database_url: str) -> str:
    """`postgresql+asyncpg://...` (app.config.Settings.DATABASE_URL) ->
    plain `postgresql://...` (what psycopg/AsyncPostgresSaver/AsyncPostgresStore
    expect). A no-op if the URL is already driver-unqualified.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def checkpointer_lifespan(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Open the checkpointer's connection and run its one-time `.setup()`
    (creates `checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/
    `checkpoint_migrations` if they don't exist yet -- idempotent, safe to
    call on every startup, see ARCHITECTURE.md §2).

    Meant to be entered once via `contextlib.AsyncExitStack` in
    app/main.py's lifespan and kept open for the process lifetime (the
    checkpointer needs a live connection for every `/chat` request), not
    re-entered per request.
    """
    dsn = to_psycopg_dsn(database_url)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        yield saver
