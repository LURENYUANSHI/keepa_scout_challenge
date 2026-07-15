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
          `tool_event_sink` hook) -- not held back until every tool call in
          the turn has finished.
    {"type": "answer", "content": "..."}             (once, final answer)
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
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
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


async def ensure_session_ownership(db: AsyncSession, session_id: str, user: User) -> None:
    """Shared ownership check (ARCHITECTURE.md §2/§3.2: `chat_sessions` is
    ownership-ONLY, not a state store) -- creates the row on first use,
    otherwise 403s if `session_id` belongs to a different user. Used by
    both `POST /chat` and `WS /chat/stream` so this logic exists in exactly
    one place."""
    session_row = await db.get(ChatSession, session_id)
    if session_row is None:
        db.add(ChatSession(session_id=session_id, user_id=user.id))
        await db.commit()
    elif session_row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This session_id belongs to a different user.",
        )


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
    await ensure_session_ownership(db, body.session_id, user)

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


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_json(payload)


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
    events to `websocket` as they happen (not after the whole turn
    finishes), then the final answer + session_state.

    `graph.ainvoke()` blocks until the entire turn (every tool round) is
    done -- there's no way to get events out of it mid-flight other than a
    concurrency bridge. So this runs `ainvoke()` as a background task and
    threads an `asyncio.Queue`-backed sink into it via
    `config["configurable"]["tool_event_sink"]`
    (app/agent/graph.py's `tools_node` calls it once per individual tool
    call, before and after dispatch); this coroutine concurrently drains
    the queue and forwards each item to the websocket the moment it
    arrives, which is what actually makes the tool events incremental on
    the wire instead of buffered until `ainvoke()` returns.
    """
    collector = TokenUsageCollector()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def sink(event: dict[str, Any]) -> None:
        await queue.put(event)

    config = {
        "configurable": {
            "thread_id": body.session_id,
            "user_id": str(user.id),
            "tool_event_sink": sink,
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
    answer = last_ai_text(result_state["messages"])
    await _send_json(websocket, {"type": "answer", "content": answer})
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
                await ensure_session_ownership(db, body.session_id, user)
            except HTTPException as exc:
                await _send_error(websocket, str(exc.detail))
                continue

            await _run_streaming_turn(websocket=websocket, graph=graph, db=db, user=user, body=body)
    except WebSocketDisconnect:
        pass
