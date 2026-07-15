"""POST /chat + WS /chat/stream — see ARCHITECTURE.md §3.3 and HARNESS.md §10.3.

Deliberately thin per ARCHITECTURE.md §5's file-layout note: this router
only does auth + `chat_sessions` ownership bookkeeping + handing the
request to app/agent/graph.py's compiled graph. All the actual
orchestration (tool calls, checkpointer/store reads, state merging) lives
in app/agent/.

WS /chat/stream protocol (HARNESS.md §10.3: tool calls must be pushed to the
frontend one at a time, not batched until the whole turn finishes):

- Connect: `ws://.../chat/stream?token=<access_token>`. Query param, not a
  message field or header -- a browser `new WebSocket(url)` call cannot set
  a custom `Authorization` header the way `fetch` can, and putting the token
  in the URL means the client doesn't need an extra handshake round-trip
  before it can start sending turns. `app.auth.dependencies.get_user_by_token`
  is the same `auth_tokens` lookup `get_current_user` does, factored out to
  take a raw string instead of a `Request`/`Header`. Missing/invalid token ->
  the server closes the handshake (code 4401) without ever `.accept()`-ing.
- Once connected, the client sends one JSON text message per turn:
  `{"session_id": "...", "message": "..."}` (`app.schemas.chat.ChatRequest`,
  the same schema `POST /chat` uses). The connection stays open across many
  turns until the client disconnects.
- Per turn, the server sends a sequence of JSON text messages:
    {"type": "tool_call_start", "tool": "<name>", "args": {...}}
    {"type": "tool_call_result", "tool": "<name>", "result": {...}}
        ^ one start/result pair per individual tool call, emitted as each
          one actually happens (see app/agent/graph.py's tools_node
          `event_sink` hook) -- not held back until every tool call in
          the turn has finished.
    {"type": "answer_delta", "content": "<chunk text>"}
        ^ zero or more, one per token/token-chunk of the final natural-
          language answer, emitted the moment app/agent/graph.py's
          `agent_node` streams them from the LLM (see
          `_stream_agent_response`) -- NOT held back until generation
          finishes. Meant to only ever be the user-facing final answer, not
          an intermediate tool-call-selection turn -- but see
          `answer_retract` below for the one case that isn't guaranteed.
    {"type": "answer_done"}                          (end of a real answer
                                                       stream -- tells the
                                                       client to stop
                                                       appending further
                                                       deltas into that
                                                       bubble and do any
                                                       final render)
    {"type": "answer_retract"}                       (rare: some
                                                       answer_delta events
                                                       were already sent for
                                                       what turned out to
                                                       actually be a
                                                       tool-call turn, not
                                                       the final answer --
                                                       SYSTEM_PROMPT tells
                                                       the model not to
                                                       narrate before
                                                       calling a tool, but a
                                                       real run showed it
                                                       doing so anyway
                                                       sometimes; the client
                                                       must discard that
                                                       bubble entirely, not
                                                       finalize it)
    {"type": "session_state", "state": {...}}          (once, end of turn;
                                                         same shape as
                                                         POST /chat's
                                                         `session_state`)
  or, on failure anywhere in the turn:
    {"type": "error", "detail": "..."}
  A turn-level error does not close the connection -- the client can send
  another `{"session_id", "message"}` for the next turn. Only a client
  disconnect (or an auth/protocol-level failure) closes the socket.
"""
import asyncio
import contextlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import extract_results, last_ai_text
from app.agent.usage import TokenUsageCollector, log_usage
from app.auth.dependencies import get_current_user, get_user_by_token
from app.config import settings
from app.db import get_db
from app.models.chat import ChatSession
from app.models.user import User
from app.schemas.chat import ChatRequest

router = APIRouter(tags=["chat"])

def _derive_title(message: str) -> str | None:
    """The user's first message in a session, stored IN FULL -- truncation
    is a presentation concern, not a storage one (the sidebar clips with a
    CSS ellipsis; clipping here would throw away data the display layer can
    never get back). `title` is a TEXT column, so length is a non-issue.
    Returns `None` for an empty/whitespace-only message (leaves `title` NULL
    rather than storing an empty string, so the frontend's "New
    conversation" fallback still kicks in)."""
    text = message.strip()
    return text or None


async def ensure_session_ownership(
    db: AsyncSession, session_id: str, user: User, message: str | None = None
) -> None:
    """Shared ownership check (ARCHITECTURE.md §2/§3.2: `chat_sessions` is
    ownership-ONLY, not a state store) -- creates the row on first use,
    otherwise 403s if `session_id` belongs to a different user. Used by
    both `POST /chat` and `WS /chat/stream` so this logic exists in exactly
    one place.

    Also does this feature's session-list bookkeeping in the same place
    rows get created/looked up (per the phase brief): `updated_at` is
    touched to "now" on creation AND on every subsequent turn for an
    existing session, and `title` is set exactly once -- the full text of
    the first `message` (see `_derive_title`) -- the moment it's still
    NULL, never overwritten by a later turn's message. `message` is
    optional (callers that aren't
    inside an actual chat turn, if any ever call this, simply skip the
    title-setting part) but both existing call sites always pass one.
    """
    now = datetime.now(timezone.utc)
    session_row = await db.get(ChatSession, session_id)
    if session_row is None:
        db.add(
            ChatSession(
                session_id=session_id,
                user_id=user.id,
                title=_derive_title(message) if message else None,
                updated_at=now,
            )
        )
        await db.commit()
    elif session_row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This session_id belongs to a different user.",
        )
    else:
        session_row.updated_at = now
        if session_row.title is None and message:
            session_row.title = _derive_title(message)
        await db.commit()


def _session_state(result_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_filters": result_state.get("active_filters", {}),
        "last_result_asins": result_state.get("last_result_asins", []),
        "resolved_entity": result_state.get("resolved_entity"),
    }


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await ensure_session_ownership(db, body.session_id, user, body.message)

    graph = request.app.state.agent_graph
    collector = TokenUsageCollector()
    config = {
        "configurable": {
            "thread_id": body.session_id,
            "user_id": str(user.id),
        },
        "callbacks": [collector],
    }

    result_state = await graph.ainvoke(
        {"messages": [HumanMessage(content=body.message)]}, config=config
    )

    answer = last_ai_text(result_state["messages"])
    results = extract_results(result_state["messages"])
    session_state = _session_state(result_state)

    await log_usage(
        db,
        user_id=user.id,
        session_id=body.session_id,
        endpoint="chat",
        model=settings.LLM_MODEL,
        collector=collector,
    )

    return {"answer": answer, "results": results, "session_state": session_state}


def _json_default(obj: Any) -> Any:
    """`json.dumps` doesn't know Decimal (Postgres NUMERIC columns come back
    as Decimal via asyncpg/SQLAlchemy) or datetime -- tool results from
    run_readonly_sql/build_filter_sql/lookup_asin can contain either.
    Starlette's WebSocket.send_json has no hook for a custom encoder, so
    _send_json bypasses it and serializes manually instead.

    Without this, a tool result containing so much as one Decimal (e.g.
    computed_roi_pct) raises TypeError deep inside send_json, which is
    unhandled and kills the whole WebSocket connection mid-turn with no
    close frame -- not a graceful error event, a hard drop. This was a real
    bug, not a hypothetical: reproduced live via a Chinese-language question
    that triggered a self-correcting multi-tool-call SQL sequence pulling
    computed_roi_pct/amazon_buybox_pct/supplier_cost."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=_json_default)
    await websocket.send_text(text)


async def _send_error(websocket: WebSocket, detail: str) -> None:
    await _send_json(websocket, {"type": "error", "detail": detail})


async def _run_streaming_turn(
    *,
    websocket: WebSocket,
    graph: Any,
    db: AsyncSession,
    user: User,
    body: ChatRequest,
) -> None:
    """Runs one `/chat` turn, forwarding tool_call_start/tool_call_result
    events AND the final answer's answer_delta/answer_done events to
    `websocket` as they happen (not after the whole turn finishes), then
    session_state.

    `graph.ainvoke()` blocks until the entire turn (every tool round) is
    done -- there's no way to get events out of it mid-flight other than a
    concurrency bridge. So this runs `ainvoke()` as a background task and
    threads an `asyncio.Queue`-backed sink into it via
    `config["configurable"]["event_sink"]` (app/agent/graph.py's
    `tools_node` calls it once per individual tool call, before and after
    dispatch; `agent_node`/`_stream_agent_response` calls it once per token
    of the final answer -- see that module for how it tells a tool-call-
    selection LLM turn's tokens apart from the user-facing answer's); this
    coroutine concurrently drains the queue and forwards each item to the
    websocket the moment it arrives, which is what actually makes both the
    tool events AND the answer text incremental on the wire instead of
    buffered until `ainvoke()` returns.
    """
    collector = TokenUsageCollector()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    answer_streamed = False

    async def sink(event: dict[str, Any]) -> None:
        await queue.put(event)

    config = {
        "configurable": {
            "thread_id": body.session_id,
            "user_id": str(user.id),
            "event_sink": sink,
        },
        "callbacks": [collector],
    }

    async def runner() -> None:
        try:
            result_state = await graph.ainvoke(
                {"messages": [HumanMessage(content=body.message)]}, config=config
            )
        except Exception as exc:  # noqa: BLE001 -- turned into a WS error event below
            await queue.put({"__error__": str(exc)})
        else:
            await queue.put({"__done__": result_state})

    task = asyncio.create_task(runner())
    try:
        result_state: dict[str, Any] | None = None
        turn_error: str | None = None
        while True:
            item = await queue.get()
            if "__done__" in item:
                result_state = item["__done__"]
                break
            if "__error__" in item:
                turn_error = item["__error__"]
                break
            item_type = item.get("type")
            if item_type == "answer_done":
                # Only a real, completed answer stream counts -- NOT a mere
                # answer_delta, since a run of deltas can still end in an
                # answer_retract (app/agent/graph.py's
                # `_stream_agent_response`: the model narrated before an
                # eventual tool call) rather than an answer_done.
                answer_streamed = True
            elif item_type == "answer_retract":
                answer_streamed = False
            await _send_json(websocket, item)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    await log_usage(
        db,
        user_id=user.id,
        session_id=body.session_id,
        endpoint="chat_stream",
        model=settings.LLM_MODEL,
        collector=collector,
    )

    if turn_error is not None:
        await _send_error(websocket, f"Turn failed: {turn_error}")
        return

    assert result_state is not None
    if not answer_streamed:
        # Defensive fallback -- should not happen on the normal path (every
        # turn that ends in a real user-facing AIMessage streams it via
        # agent_node's event_sink hook above), but if a turn ever ends
        # without ever running a final non-tool-call agent round (e.g. it
        # hit MAX_TOOL_ROUNDS mid-tool-call and `should_continue` ended the
        # graph on an AIMessage that still has tool_calls set), fall back to
        # sending whatever text is there in one shot rather than silently
        # dropping it.
        answer = last_ai_text(result_state["messages"])
        if answer:
            await _send_json(websocket, {"type": "answer_delta", "content": answer})
            await _send_json(websocket, {"type": "answer_done"})
    await _send_json(websocket, {"type": "session_state", "state": _session_state(result_state)})


@router.websocket("/chat/stream")
async def chat_stream(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    token = websocket.query_params.get("token")
    user = await get_user_by_token(token, db)
    if user is None:
        # Reject the handshake itself (never `.accept()`-ed) -- the client
        # sees the connection refused/closed immediately rather than hanging
        # or getting a 200 that then errors on the first message.
        await websocket.close(code=4401)
        return

    await websocket.accept()

    graph = websocket.app.state.agent_graph

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:  # noqa: BLE001 -- malformed frame, not a disconnect
                await _send_error(websocket, "Message must be valid JSON.")
                continue

            try:
                body = ChatRequest.model_validate(raw)
            except ValidationError as exc:
                await _send_error(websocket, f"Invalid message: {exc}")
                continue

            try:
                await ensure_session_ownership(db, body.session_id, user, body.message)
            except HTTPException as exc:
                await _send_error(websocket, str(exc.detail))
                continue

            await _run_streaming_turn(websocket=websocket, graph=graph, db=db, user=user, body=body)
    except WebSocketDisconnect:
        pass


# --- session list + resume (history replay) -------------------------------
#
# Everything below is new surface for the "browse/resume past conversations"
# feature: `chat_sessions` already tracked ownership + (as of this feature)
# title/updated_at, but nothing previously read a thread's actual message
# history back OUT of the checkpointer -- POST /chat and WS /chat/stream
# only ever write to it (via `graph.ainvoke(..., config=...)`, which
# implicitly persists through the checkpointer passed to `build_graph` in
# app/main.py's lifespan). `GET /chat/sessions/{id}/messages` below is the
# first place this repo reads checkpointed state back out, via
# `CompiledStateGraph.aget_state(config)` -- verified directly against the
# installed langgraph==0.2.76 (`langgraph.pregel.Pregel.aget_state`, the
# base class `StateGraph.compile()` returns), not assumed from newer docs:
# it returns a `StateSnapshot` namedtuple whose `.values` is the exact same
# dict shape `graph.ainvoke()` resolves to (confirmed empirically against a
# real checkpointed thread from this dev DB -- `.values["messages"]` is a
# plain list of LangChain message objects, in the same append order
# `agent_node`/`tools_node` produced them turn over turn). For a thread_id
# with no checkpoint yet, `.values` comes back as `{}` (also confirmed
# empirically) -- so `.get("messages", [])` naturally yields `[]` rather
# than raising, which is exactly the "exists but empty -> [], not 404"
# behavior the phase brief asks for.


def _text_content(content: Any) -> str:
    """Best-effort plain text out of a LangChain message's `.content` --
    mirrors app/agent/graph.py's `last_ai_text`/`_chunk_text` handling of
    the same "usually a str, sometimes a list of content blocks"
    provider-dependent shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _replay_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Converts a checkpointed thread's raw LangChain message list (see the
    module-level note above for exactly how it's fetched) into the SAME
    `{"type": "user"|"tool"|"answer", ...}` shape
    frontend/src/views/Chat.vue's `messages` array already uses for live
    turns, so replayed history renders through the exact same templates --
    no parallel markup.

    Shape of the source list, per app/agent/graph.py: HumanMessage (one per
    user turn) / AIMessage with `tool_calls` set (a tool-selection round,
    never the user-facing answer) / ToolMessage (one per dispatched tool
    call, linked back to its call via `tool_call_id`) / AIMessage with no
    `tool_calls` (the turn's real final answer). SystemMessage never lands
    in checkpointed state -- `agent_node` builds one fresh per LLM call but
    only ever returns the AIMessage response into state -- so there's
    nothing to filter there, but a stray one is skipped defensively rather
    than crashing this endpoint.

    A tool-calling AIMessage's own `.content` (the occasional narration text
    the model produces despite SYSTEM_PROMPT telling it not to -- see
    graph.py's `_stream_agent_response` docstring) is intentionally NOT
    replayed as an `answer` bubble: on the live path that exact text either
    never streamed at all (system prompt worked that turn) or got sent as
    `answer_delta`s and then discarded via `answer_retract` (system prompt
    didn't) -- either way it was never meant to persist as a visible
    message, so replay shouldn't resurrect it. Each tool_call becomes its
    own `type: "tool"` entry (`status: "done"` -- this is history, nothing
    is still running), matched to its result by `tool_call_id` rather than
    list position, since parallel tool calls don't guarantee the following
    ToolMessage(s) arrive in the same order the calls were made.
    """
    out: list[dict[str, Any]] = []
    pending_by_call_id: dict[str, dict[str, Any]] = {}
    seq = 0

    for message in messages:
        if isinstance(message, HumanMessage):
            seq += 1
            out.append({"id": f"h{seq}", "type": "user", "content": _text_content(message.content)})
        elif isinstance(message, AIMessage):
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                for call in tool_calls:
                    seq += 1
                    entry = {
                        "id": f"h{seq}",
                        "type": "tool",
                        "tool": call.get("name"),
                        "args": call.get("args") or {},
                        "status": "done",
                        "result": None,
                    }
                    out.append(entry)
                    call_id = call.get("id")
                    if call_id:
                        pending_by_call_id[call_id] = entry
            else:
                text = _text_content(message.content)
                if text.strip():
                    seq += 1
                    out.append({"id": f"h{seq}", "type": "answer", "content": text, "streaming": False})
        elif isinstance(message, ToolMessage):
            entry = pending_by_call_id.get(message.tool_call_id)
            if entry is not None:
                raw = message.content
                try:
                    entry["result"] = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    entry["result"] = raw

    return out


async def _backfill_title_from_checkpoint(graph: Any, row: ChatSession) -> None:
    """`ensure_session_ownership` normally sets `title` from the very first
    message at creation time, but rows created before that logic existed
    (or before `message` reached it for some other reason -- a crashed
    first turn, a raw/non-UI client) are left with `title IS NULL` forever,
    since nothing else ever re-derives it. Rather than showing every such
    row as an undifferentiated "New conversation" indefinitely, fall back
    to the same source `GET /chat/sessions/{id}/messages` already reads --
    the checkpointer's own message history -- and derive it from the first
    HumanMessage there, exactly like a normal first turn would have. Mutates
    `row.title` in place; caller is responsible for committing."""
    snapshot = await graph.aget_state({"configurable": {"thread_id": row.session_id}})
    for message in (snapshot.values or {}).get("messages", []):
        if isinstance(message, HumanMessage):
            title = _derive_title(_text_content(message.content))
            if title:
                row.title = title
            break


@router.get("/chat/sessions")
async def list_chat_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The current user's sessions, most-recently-active first (`updated_at`
    DESC, `created_at` DESC as the tiebreak/fallback -- spelled out with
    `nullslast()` per the phase brief even though `updated_at` is NOT NULL
    end-to-end today, since a row from before this feature shipped could in
    principle still have a NULL `updated_at` if the manual `ALTER TABLE`
    backfill ever gets skipped in some other environment).

    `title` can be null (the row exists -- `ensure_session_ownership`
    creates it eagerly -- but no title-worthy message has landed yet, e.g.
    the turn crashed before completion, or a race, or the row predates
    title-on-creation altogether). Rather than show a permanent generic
    "New conversation" for those, backfill from the checkpointed history
    (see `_backfill_title_from_checkpoint`) once here and persist it, so
    this only ever runs once per stale row.
    """
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc().nullslast(), ChatSession.created_at.desc())
    )
    rows = result.scalars().all()

    untitled = [row for row in rows if row.title is None]
    if untitled:
        graph = request.app.state.agent_graph
        for row in untitled:
            await _backfill_title_from_checkpoint(graph, row)

    # Payload is built BEFORE the commit below, deliberately: committing an
    # UPDATE on a row whose `updated_at` has a server-side onupdate expires
    # that attribute on the ORM instance (SQLAlchemy can't know the value
    # the server just generated), and touching an expired attribute after
    # the await would need a sync refresh -> MissingGreenlet under async
    # SQLAlchemy. Reading everything first sidesteps that entirely, and the
    # response stays consistent with the ordering this query just returned.
    payload = [
        {
            "session_id": row.session_id,
            "title": row.title,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]
    if untitled:
        await db.commit()
    return payload


@router.get("/chat/sessions/{session_id}/messages")
async def get_chat_session_messages(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Full replayed message history for one session.

    Ownership-checked inline rather than via `ensure_session_ownership`
    (that helper only ever creates-a-row-or-403s -- it has no "the row
    doesn't exist and shouldn't be created" outcome, which is exactly what
    a GET needs: no side-effecting row creation for a session nobody has
    ever written to). 403 if the session exists but belongs to a different
    user.

    A session_id with no row at all returns an empty list, NOT a 404: with
    URL-based routing (`/chat/:sessionId`, where bare `/chat` redirects to a
    freshly minted id), a never-used session id is a completely normal
    state the frontend hits on every single "New chat" -- it genuinely is
    "a conversation with no messages yet", and treating it as an error
    would (and did) spam the browser console with a red
    `Failed to load resource: 404` on every new-chat page load even though
    the frontend handled it gracefully. No information is leaked by the
    200: a probed foreign session id that EXISTS still 403s below, and one
    that doesn't exist looks identical to one the prober could "create"
    themselves anyway (rows are minted lazily on first message,
    per-owner). Same reasoning as the exists-but-no-checkpoint case, which
    also returns [].
    """
    session_row = await db.get(ChatSession, session_id)
    if session_row is None:
        return []
    if session_row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This session_id belongs to a different user.",
        )

    graph = request.app.state.agent_graph
    snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
    messages = (snapshot.values or {}).get("messages", [])
    return _replay_messages(messages)
