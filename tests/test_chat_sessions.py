"""GET /chat/sessions + GET /chat/sessions/{id}/messages -- the
session-list/resume feature. See app/routers/chat.py's `_replay_messages`
docstring for the checkpointer-read-back contract this exercises.

Same `TestClient`-with-real-lifespan pattern as tests/test_chat_websocket.py
(see that file's module docstring for the full rationale): both new
endpoints -- and `POST /chat`, which these tests use to seed real
checkpointed history -- read `app.state.agent_graph`, which only exists
once FastAPI's lifespan has actually run, which httpx's `ASGITransport`
(tests/conftest.py's `client` fixture) never does. Real DeepSeek calls
happen here on purpose, same as test_chat_websocket.py -- replaying a tool
card end-to-end is exactly the kind of thing an all-mocked test would fail
to catch (wrong `tool_call_id` matching, wrong message-type mapping, etc).
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.db import engine as production_engine
from app.db import get_db
from tests.conftest import TEST_DATABASE_URL
from tests.test_chat_websocket import _noop_init_db


def _unique_email() -> str:
    return f"sessions-user-{uuid.uuid4().hex}@example.com"


@pytest.fixture(scope="module")
def sessions_client(test_engine):
    """See tests/test_chat_websocket.py's `ws_client` fixture docstring --
    identical rationale (module-scoped TestClient/portal loop, separate
    AsyncEngine for the `get_db` override, `init_db` patched out). Kept as
    its own fixture rather than importing `ws_client` directly since pytest
    fixtures need to be requested by name in the *using* module's own scope
    to be discovered reliably."""
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
                # `app.db.engine`/`async_session_maker` (used directly by
                # app/agent/graph.py's `tools_node` for every dispatched
                # tool call, bypassing any `get_db` override) is a
                # process-wide singleton engine whose asyncpg pool binds to
                # whichever event loop first checks a connection out of it.
                # Earlier test modules in the same `pytest` run (e.g.
                # earlier real-LLM test modules whose fixtures talked to
                # this exact production engine on pytest-asyncio's
                # *session*-scoped loop, never disposing it) can leave that
                # pool populated with connections bound to a DIFFERENT loop
                # than this fixture's own dedicated TestClient portal
                # thread/loop -- the first tool-calling turn in this module
                # then hits asyncpg's "another operation is in progress" /
                # "Future attached to a different loop" the moment it tries
                # to reuse one of those stale connections. Disposing here,
                # right after entering (so it happens on THIS fixture's
                # portal loop, before any test runs), forces a clean pool
                # that only ever binds to this module's loop -- the same
                # defensive dispose tests/test_chat_websocket.py's
                # `ws_client` fixture does at teardown, just also done at
                # setup since this module can't control what ran before it.
                tc.portal.call(production_engine.dispose)
                tc.state_session_maker = session_maker  # for direct-DB-insert tests
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


def _whoami(tc: TestClient, token: str) -> str:
    resp = tc.get("/auth/_whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["user_id"]


def _send_turn(tc: TestClient, token: str, session_id: str, message: str) -> dict:
    resp = tc.post(
        "/chat",
        json={"session_id": session_id, "message": message},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- title derivation (full first message; display clips, storage doesn't) --


def test_short_message_becomes_verbatim_title(sessions_client):
    token = _register(sessions_client)
    session_id = f"title-short-{uuid.uuid4().hex}"
    message = "What does ROI mean?"

    _send_turn(sessions_client, token, session_id, message)

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] == message


def test_long_message_title_is_stored_in_full(sessions_client):
    """Truncation is a presentation concern (the sidebar clips with a CSS
    ellipsis) -- storage keeps the complete first message, because data
    clipped at write time can never be recovered by the display layer."""
    token = _register(sessions_client)
    session_id = f"title-long-{uuid.uuid4().hex}"
    message = (
        "Please show me every single eligible ASIN in my catalog sorted "
        "by return on investment percentage from highest to lowest, top ten only."
    )
    assert len(message) > 60

    _send_turn(sessions_client, token, session_id, message)

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] == message


def test_title_set_once_never_overwritten_by_later_turns(sessions_client):
    token = _register(sessions_client)
    session_id = f"title-fixed-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token, session_id, "What does ROI mean?")
    _send_turn(sessions_client, token, session_id, "And what about BuyBox share?")
    _send_turn(sessions_client, token, session_id, "One more unrelated question here.")

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] == "What does ROI mean?"


# --- ordering ---------------------------------------------------------------


def test_sessions_ordered_by_updated_at_desc(sessions_client):
    token = _register(sessions_client)
    session_a = f"order-a-{uuid.uuid4().hex}"
    session_b = f"order-b-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token, session_a, "What does ROI mean?")
    _send_turn(sessions_client, token, session_b, "What is Amazon BuyBox share?")

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    ids_in_order = [r["session_id"] for r in resp.json()]
    assert ids_in_order.index(session_b) < ids_in_order.index(session_a), (
        "session_b was touched more recently and must sort first"
    )

    # Touch session_a again -- it should now overtake session_b.
    _send_turn(sessions_client, token, session_a, "And what counts as a price anomaly?")

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    ids_in_order = [r["session_id"] for r in resp.json()]
    assert ids_in_order.index(session_a) < ids_in_order.index(session_b), (
        "session_a was just re-touched and must now sort first"
    )


def test_sessions_list_only_returns_the_caller_own_sessions(sessions_client):
    token_a = _register(sessions_client)
    token_b = _register(sessions_client)
    session_a = f"scope-a-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token_a, session_a, "What does ROI mean?")

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token_b}"})
    ids = [r["session_id"] for r in resp.json()]
    assert session_a not in ids


# --- GET /chat/sessions/{id}/messages: replay shape -------------------------


def test_messages_replays_user_and_answer_for_a_plain_turn(sessions_client):
    token = _register(sessions_client)
    session_id = f"replay-plain-{uuid.uuid4().hex}"
    message = "What does ROI mean?"

    _send_turn(sessions_client, token, session_id, message)

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    history = resp.json()

    assert history[0]["type"] == "user"
    assert history[0]["content"] == message

    answer_entries = [m for m in history if m["type"] == "answer"]
    assert len(answer_entries) == 1
    assert answer_entries[0]["streaming"] is False
    assert answer_entries[0]["content"].strip()

    # No tool call was needed for a definitional question -- no tool cards.
    assert not any(m["type"] == "tool" for m in history)


def test_messages_replays_tool_call_card_with_result(sessions_client):
    token = _register(sessions_client)
    session_id = f"replay-tool-{uuid.uuid4().hex}"

    _send_turn(
        sessions_client,
        token,
        session_id,
        "Show me eligible ASINs sorted by ROI, top 3 only.",
    )

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    history = resp.json()

    tool_entries = [m for m in history if m["type"] == "tool"]
    assert len(tool_entries) >= 1, f"expected at least one tool card; got: {history}"
    tool_entry = tool_entries[0]
    assert tool_entry["tool"] == "build_filter_sql"
    assert tool_entry["status"] == "done"
    assert isinstance(tool_entry["args"], dict)
    assert isinstance(tool_entry["result"], dict)
    assert "rows" in tool_entry["result"]

    # A user bubble precedes the tool card, and a final answer follows it.
    # A pre-tool answer segment (the model writing the first part of its
    # answer before the tool call) may ALSO appear before the tool card --
    # _replay_messages replays it -- so compare against the LAST answer.
    types = [m["type"] for m in history]
    assert types[0] == "user"
    assert "answer" in types
    last_answer_idx = len(types) - 1 - types[::-1].index("answer")
    assert types.index("tool") < last_answer_idx


def test_messages_two_turns_appends_after_replayed_history(sessions_client):
    """Resuming a session and sending a further turn must append, not
    replace -- covered here at the API level (both turns' messages show up,
    in order, in one replay call)."""
    token = _register(sessions_client)
    session_id = f"replay-two-turns-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token, session_id, "What does ROI mean?")
    _send_turn(sessions_client, token, session_id, "And what counts as a price anomaly?")

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    history = resp.json()
    user_messages = [m["content"] for m in history if m["type"] == "user"]
    assert user_messages == ["What does ROI mean?", "And what counts as a price anomaly?"]


# --- ownership / not-found ---------------------------------------------------


def test_messages_403_for_a_different_users_session(sessions_client):
    token_a = _register(sessions_client)
    token_b = _register(sessions_client)
    session_id = f"replay-403-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token_a, session_id, "What does ROI mean?")

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 403


def test_messages_empty_list_for_a_session_that_was_never_created(sessions_client):
    """A never-used session id is a first-class state under URL routing --
    every "New chat" navigates to a freshly minted id and immediately fetches
    its history -- so it must be 200 + [], not a 404 (which would log a red
    `Failed to load resource` in the browser console on every new chat even
    though the frontend tolerates it). See get_chat_session_messages's
    docstring for why this leaks nothing."""
    token = _register(sessions_client)
    session_id = f"replay-fresh-{uuid.uuid4().hex}"

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_messages_empty_list_for_existing_session_with_no_checkpoint(sessions_client):
    """A `chat_sessions` row can exist (e.g. `ensure_session_ownership` ran)
    without any checkpointed graph state yet (e.g. the turn crashed before
    ever reaching `graph.ainvoke`) -- that must be an empty list, not a 404
    or a 500."""
    token = _register(sessions_client)
    user_id = _whoami(sessions_client, token)
    session_id = f"replay-empty-{uuid.uuid4().hex}"

    async def _insert_orphan_session():
        from app.models.chat import ChatSession

        async with sessions_client.state_session_maker() as session:
            session.add(ChatSession(session_id=session_id, user_id=uuid.UUID(user_id)))
            await session.commit()

    sessions_client.portal.call(_insert_orphan_session)

    resp = sessions_client.get(
        f"/chat/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_and_messages_endpoints_require_auth(sessions_client):
    resp = sessions_client.get("/chat/sessions")
    assert resp.status_code == 401

    resp = sessions_client.get(f"/chat/sessions/{uuid.uuid4().hex}/messages")
    assert resp.status_code == 401


# --- NULL-title backfill from checkpointed history --------------------------
# `_backfill_title_from_checkpoint`: rows from before title-on-creation
# existed (or whose first turn never set one) get their title re-derived from
# the checkpointer's first HumanMessage the first time GET /chat/sessions
# sees them -- and persisted, so it's a one-time repair per row.


def _set_title_directly(tc: TestClient, session_id: str, title) -> None:
    from sqlalchemy import update

    from app.models.chat import ChatSession

    async def _update():
        async with tc.state_session_maker() as session:
            await session.execute(
                update(ChatSession).where(ChatSession.session_id == session_id).values(title=title)
            )
            await session.commit()

    tc.portal.call(_update)


def _get_title_directly(tc: TestClient, session_id: str):
    from app.models.chat import ChatSession

    async def _read():
        async with tc.state_session_maker() as session:
            row = await session.get(ChatSession, session_id)
            return row.title

    return tc.portal.call(_read)


def test_null_title_is_backfilled_from_checkpointed_first_message(sessions_client):
    token = _register(sessions_client)
    session_id = f"backfill-{uuid.uuid4().hex}"
    message = "What does ROI mean?"

    _send_turn(sessions_client, token, session_id, message)
    # Simulate a legacy row: title never got set despite real history.
    _set_title_directly(sessions_client, session_id, None)
    assert _get_title_directly(sessions_client, session_id) is None

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] == message

    # Persisted, not just decorated onto the response.
    assert _get_title_directly(sessions_client, session_id) == message


def test_null_title_with_no_checkpoint_stays_null_without_crashing(sessions_client):
    token = _register(sessions_client)
    user_id = _whoami(sessions_client, token)
    session_id = f"backfill-empty-{uuid.uuid4().hex}"

    async def _insert_orphan_session():
        from app.models.chat import ChatSession

        async with sessions_client.state_session_maker() as session:
            session.add(ChatSession(session_id=session_id, user_id=uuid.UUID(user_id)))
            await session.commit()

    sessions_client.portal.call(_insert_orphan_session)

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] is None
    assert _get_title_directly(sessions_client, session_id) is None


def test_existing_title_is_never_overwritten_by_backfill(sessions_client):
    token = _register(sessions_client)
    session_id = f"backfill-keep-{uuid.uuid4().hex}"

    _send_turn(sessions_client, token, session_id, "What does ROI mean?")
    # A user-visible title already exists (here: manually customized, which
    # differs from the checkpointed first message) -- listing must not
    # "repair" it back.
    _set_title_directly(sessions_client, session_id, "my custom label")

    resp = sessions_client.get("/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    rows = {r["session_id"]: r for r in resp.json()}
    assert rows[session_id]["title"] == "my custom label"
