"""WS /chat/stream — see app/routers/chat.py's module docstring for the
protocol and HARNESS.md §10.3 for why it needs to exist (tool calls must be
pushed to the frontend one at a time, not batched until the whole turn
finishes).

Why this file uses `starlette.testclient.TestClient` (sync) instead of the
`client` fixture (httpx `AsyncClient` over `ASGITransport`) every other test
file uses: httpx's `AsyncClient`/`ASGITransport` doesn't support WebSocket
upgrades at all -- `TestClient.websocket_connect()` is the supported way to
drive a WS route in-process, and it works fine against an async FastAPI app
despite being a sync API (it drives the app from its own dedicated
anyio portal thread/event loop).

That portal thread is exactly why this file can't just reuse the session-
scoped `test_engine` fixture from tests/conftest.py directly: `test_engine`'s
asyncpg connections get bound to whichever event loop first uses them (the
pytest-asyncio *session* loop, since `test_engine` is session-scoped and
other async tests touch it first), and asyncpg raises "Future attached to a
different loop" the moment a *different* loop (TestClient's portal loop)
tries to reuse a pooled connection. The `ws_client` fixture below still
depends on `test_engine` (so schema creation has definitely already
happened against the real test database), but builds its own separate
`AsyncEngine` object for the `get_db` override -- that engine is never
touched by any coroutine until TestClient's portal thread is the one
running it, so it only ever binds to one loop.

Also note: unlike the `client` fixture, entering `with TestClient(app):`
*does* run FastAPI's lifespan (startup/shutdown) -- required here because
`app.state.agent_graph` (the compiled LangGraph graph WS /chat/stream reads
off `websocket.app.state`) is only ever built inside that lifespan
(app/main.py). Real DeepSeek calls happen in these tests on purpose per this
phase's instructions -- the thing being verified (incremental event
ordering) is exactly the kind of behavior an all-mocked test would fail to
catch.
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.db import engine as production_engine
from app.db import get_db
from tests.conftest import TEST_DATABASE_URL


async def _noop_init_db() -> None:
    """Stand-in for `app.main.init_db` during this test module only.

    `app.main.lifespan()`'s real `init_db()` (app/db.py) issues a DDL
    reflection query (`has_table`) through `app.db.engine` -- the
    production, asyncpg-backed `AsyncEngine`, whose async driver goes
    through SQLAlchemy's greenlet sync-to-async bridge. That bridge doesn't
    tolerate running inside `starlette.testclient.TestClient`'s
    `anyio`-portal-driven lifespan invocation (`portal.call(self.wait_startup)`
    spawns it inside an anyio TaskGroup task, and asyncpg's own Future/Task
    bookkeeping ends up seeing two different loops -- confirmed empirically:
    it fails on the very first `has_table` call, every time, regardless of
    whether the tables already exist) -- a TestClient-specific
    incompatibility between two `anyio` structured-concurrency layers, not a
    bug in `init_db()` itself (the real `api` service's normal uvicorn
    lifespan runs the identical call successfully on every `docker compose
    up`, which is how the `keepa_scout` database this test module also
    talks to already has all its tables). Skipping it here is safe: those
    tables already exist in that shared database by the time this test
    module runs (this repo's `docker compose up` / test-verification flow
    always brings the `api` service up first).
    """
    return None


def _unique_email() -> str:
    return f"ws-user-{uuid.uuid4().hex}@example.com"


@pytest.fixture(scope="module")
def ws_client(test_engine):
    """Module-scoped (not per-test): `with TestClient(app):` runs FastAPI's
    real lifespan (startup/shutdown) on its own dedicated portal
    thread/event loop -- required here because `app.state.agent_graph` only
    gets built inside that lifespan (app/main.py). If this fixture were
    function-scoped, every test would enter/exit a *fresh* portal loop while
    `app.db.engine` (the module-level production engine `init_db()` runs
    against on every lifespan startup) is a process-wide singleton whose
    connection pool doesn't get disposed between cycles -- a pooled asyncpg
    connection opened under test N's now-dead loop then gets handed to test
    N+1's new loop and fails with "another operation is in progress"
    (observed empirically). One TestClient/one portal loop for the whole
    file avoids that entirely; per-test isolation instead comes from each
    test using its own freshly-registered user(s) and uuid4 session_ids.
    """
    from app.main import app

    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db

    try:
        with patch("app.main.init_db", _noop_init_db):
            with TestClient(app) as tc:
                yield tc
                tc.portal.call(engine.dispose)
                tc.portal.call(production_engine.dispose)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _register(tc: TestClient) -> str:
    email = _unique_email()
    resp = tc.post("/auth/register", json={"email": email, "password": "correct horse battery"})
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def test_ws_stream_emits_tool_events_before_answer_and_session_state(ws_client):
    token = _register(ws_client)
    session_id = f"ws-test-{uuid.uuid4().hex}"

    with ws_client.websocket_connect(f"/chat/stream?token={token}") as ws:
        ws.send_json(
            {
                "session_id": session_id,
                "message": "Show me eligible ASINs sorted by ROI, top 5 only.",
            }
        )

        events = []
        for _ in range(50):  # hard cap so a protocol bug can't hang the test
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "session_state":
                break
        else:
            pytest.fail(f"never received a session_state event; got: {events}")

    types = [e["type"] for e in events]
    assert "error" not in types, f"unexpected error event(s): {events}"
    assert "tool_call_start" in types, f"expected a tool call; got: {events}"
    assert "tool_call_result" in types, f"expected a tool result; got: {events}"
    assert "answer" in types
    assert "session_state" in types

    # Ordering: each tool_call_start precedes its matching tool_call_result,
    # and both precede the final answer/session_state (HARNESS.md §10.3 --
    # events must arrive one at a time, not all dumped together at the end).
    start_idx = types.index("tool_call_start")
    result_idx = types.index("tool_call_result")
    answer_idx = types.index("answer")
    state_idx = types.index("session_state")
    assert start_idx < result_idx < answer_idx < state_idx

    tool_call_start = events[start_idx]
    assert tool_call_start["tool"]
    assert "args" in tool_call_start
    tool_call_result = events[result_idx]
    assert tool_call_result["tool"] == tool_call_start["tool"]
    assert "result" in tool_call_result

    answer_event = events[answer_idx]
    assert isinstance(answer_event["content"], str) and answer_event["content"].strip()

    state_event = events[state_idx]
    assert "active_filters" in state_event["state"]
    assert "last_result_asins" in state_event["state"]
    assert "resolved_entity" in state_event["state"]

    # The connection must stay open for a second turn on the same socket
    # (a real chat UI sends many messages over one connection, not a
    # connect-per-turn) -- verified below in the two-turn test, kept
    # separate here to keep this test focused on ordering.


def test_ws_stream_keeps_connection_open_across_multiple_turns(ws_client):
    token = _register(ws_client)
    session_id = f"ws-test-{uuid.uuid4().hex}"

    with ws_client.websocket_connect(f"/chat/stream?token={token}") as ws:
        for message in (
            "What does ROI mean?",
            "And what counts as a price anomaly?",
        ):
            ws.send_json({"session_id": session_id, "message": message})
            saw_session_state = False
            for _ in range(50):
                event = ws.receive_json()
                if event["type"] == "error":
                    pytest.fail(f"unexpected error event: {event}")
                if event["type"] == "session_state":
                    saw_session_state = True
                    break
            assert saw_session_state


def test_ws_stream_rejects_missing_token(ws_client):
    with pytest.raises(Exception):
        with ws_client.websocket_connect("/chat/stream") as ws:
            ws.receive_json()


def test_ws_stream_rejects_invalid_token(ws_client):
    with pytest.raises(Exception):
        with ws_client.websocket_connect("/chat/stream?token=not-a-real-token") as ws:
            ws.receive_json()


def test_ws_stream_wrong_session_owner_gets_error_event_not_disconnect(ws_client):
    token_a = _register(ws_client)
    token_b = _register(ws_client)
    session_id = f"ws-test-{uuid.uuid4().hex}"

    # user A creates/owns the session first.
    with ws_client.websocket_connect(f"/chat/stream?token={token_a}") as ws:
        ws.send_json({"session_id": session_id, "message": "What does ROI mean?"})
        for _ in range(50):
            event = ws.receive_json()
            if event["type"] == "session_state":
                break

    # user B tries to use the same session_id -- must get a 403-equivalent
    # error event, and the socket must stay usable for a *different*
    # session_id afterward rather than just dying.
    with ws_client.websocket_connect(f"/chat/stream?token={token_b}") as ws:
        ws.send_json({"session_id": session_id, "message": "hi"})
        error_event = ws.receive_json()
        assert error_event["type"] == "error"
        assert "different user" in error_event["detail"].lower() or "belongs" in error_event["detail"].lower()

        own_session_id = f"ws-test-{uuid.uuid4().hex}"
        ws.send_json({"session_id": own_session_id, "message": "What does ROI mean?"})
        for _ in range(50):
            event = ws.receive_json()
            if event["type"] == "session_state":
                break
        else:
            pytest.fail("connection did not recover for a fresh session_id after the ownership error")
