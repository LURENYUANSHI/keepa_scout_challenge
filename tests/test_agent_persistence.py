"""Round-trip tests for LangGraph's checkpointer (short-term memory, keyed by
thread_id) and store (long-term memory, keyed by ("preferences", user_id)).

See ARCHITECTURE.md §2/§4 and app/agent/checkpointer.py / app/agent/store.py.
graph.py doesn't exist as an importable dependency for these tests -- we hit
the `AsyncPostgresSaver`/`AsyncPostgresStore` APIs directly (put/get) rather
than going through a compiled graph, since this phase is only responsible
for the persistence layer, not the graph itself.

Uses the same TEST_DATABASE_URL convention as tests/conftest.py (a
`_test`-suffixed DB), translated to a plain psycopg DSN via
`app.agent.checkpointer.to_psycopg_dsn` -- the same translation
`checkpointer_lifespan`/`store_lifespan` use in app/main.py's lifespan.
"""
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from app.agent.checkpointer import checkpointer_lifespan, to_psycopg_dsn
from app.agent.store import store_lifespan
from tests.conftest import TEST_DATABASE_URL

PSYCOPG_TEST_DSN = to_psycopg_dsn(TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_checkpointer_round_trip() -> None:
    """Write a trivial checkpoint under a thread_id, read it back via
    `aget_tuple`, and confirm the checkpoint id and a custom
    channel_values entry survive the round trip."""
    thread_id = f"test-thread-{uuid.uuid4()}"

    async with checkpointer_lifespan(PSYCOPG_TEST_DSN) as saver:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"greeting": "hello from test_agent_persistence"}

        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        metadata = {"source": "input", "step": 1, "writes": {}, "parents": {}}

        await saver.aput(config, checkpoint, metadata, {})

        tuple_out = await saver.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        )

        assert tuple_out is not None
        assert tuple_out.checkpoint["id"] == checkpoint["id"]
        assert tuple_out.checkpoint["channel_values"]["greeting"] == (
            "hello from test_agent_persistence"
        )
        assert tuple_out.config["configurable"]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_checkpointer_isolates_by_thread_id() -> None:
    """A checkpoint written under one thread_id must not leak into another
    thread_id's lookup -- checkpointer is short-term/thread-scoped memory,
    not global state."""
    thread_a = f"test-thread-a-{uuid.uuid4()}"
    thread_b = f"test-thread-b-{uuid.uuid4()}"

    async with checkpointer_lifespan(PSYCOPG_TEST_DSN) as saver:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"marker": "only-in-thread-a"}
        metadata = {"source": "input", "step": 1, "writes": {}, "parents": {}}

        config_a = {"configurable": {"thread_id": thread_a, "checkpoint_ns": ""}}
        await saver.aput(config_a, checkpoint, metadata, {})

        tuple_b = await saver.aget_tuple(
            {"configurable": {"thread_id": thread_b, "checkpoint_ns": ""}}
        )
        assert tuple_b is None


@pytest.mark.asyncio
async def test_store_round_trip() -> None:
    """Write a preference under namespace ("preferences", user_id), read it
    back via `aget`, and confirm the value survives the round trip."""
    user_id = f"test-user-{uuid.uuid4()}"
    namespace = ("preferences", user_id)

    async with store_lifespan(PSYCOPG_TEST_DSN) as store:
        value = {"budget_per_unit": 25.5, "excluded_asins": ["B000TESTASIN"]}
        await store.aput(namespace, "budget", value)

        item = await store.aget(namespace, "budget")

        assert item is not None
        assert item.value == value
        assert item.namespace == namespace
        assert item.key == "budget"


@pytest.mark.asyncio
async def test_store_isolates_by_namespace() -> None:
    """A value written under one user_id's namespace must not be visible
    under a different user_id's namespace -- store is long-term but
    per-user, not global."""
    user_a = f"test-user-a-{uuid.uuid4()}"
    user_b = f"test-user-b-{uuid.uuid4()}"

    async with store_lifespan(PSYCOPG_TEST_DSN) as store:
        await store.aput(("preferences", user_a), "budget", {"budget_per_unit": 10})

        item = await store.aget(("preferences", user_b), "budget")
        assert item is None
