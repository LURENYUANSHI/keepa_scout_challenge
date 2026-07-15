"""`AsyncPostgresStore` initialization — LangGraph's long-term memory.

ARCHITECTURE.md §2/§4.2: this is where `update_preferences` writes
(namespace `("preferences", user_id)`) and `plan_combo` reads from --
budget/excluded-ASINs persist across sessions (HARNESS.md §7.2 scenario E:
"换一个新 session_id（同一用户）依然生效") because the Store partitions by
user_id, not by thread_id/session_id like the checkpointer does.

Same DSN-translation note as app/agent/checkpointer.py applies here.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.store.postgres.aio import AsyncPostgresStore

from app.agent.checkpointer import to_psycopg_dsn


@asynccontextmanager
async def store_lifespan(database_url: str) -> AsyncIterator[AsyncPostgresStore]:
    """Open the store's connection and run its one-time `.setup()` (creates
    the Store's own tables -- separate from the checkpointer's, see
    ARCHITECTURE.md §2). Entered once via app/main.py's lifespan
    `AsyncExitStack`, kept open for the process lifetime.
    """
    dsn = to_psycopg_dsn(database_url)
    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()
        yield store
