"""Unit tests for app/agent/graph.py's `_stream_agent_response` event
emission and `turn_answer_text` -- deterministic, no LLM, no DB.

The regression these pin down (observed live with DeepSeek, 2026-07-16): on
a mixed "explain X, then fetch Y" question the model streams the ENTIRE
explanation half of its answer as content BEFORE emitting its tool call,
and the post-tool round then deliberately does not repeat it. The old
`answer_retract` behavior deleted that pre-tool bubble client-side, so the
user permanently lost half the answer. The contract now is:

  - content-then-tool_call stream -> the streamed deltas are FINALIZED with
    an `answer_done` (a kept, visible answer segment), never retracted;
  - `turn_answer_text` joins every AIMessage text segment of the turn, so
    non-streaming callers (POST /chat) see the same full answer the WS
    client rendered.
"""
import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)

from app.agent.graph import _stream_agent_response, turn_answer_text


class _ScriptedLLM:
    """Stands in for the bound ChatOpenAI: `astream` replays a fixed chunk
    sequence, ignoring its inputs."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def astream(self, messages, config=None):
        for chunk in self._chunks:
            yield chunk


def _tool_call_chunk(args_fragment: str, *, first: bool):
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": "build_filter_sql" if first else None,
                "args": args_fragment,
                "id": "call_1" if first else None,
                "index": 0,
            }
        ],
    )


class _SinkRecorder:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_content_then_tool_call_finalizes_segment_instead_of_retracting():
    """The narrate-then-tool stream shape: real answer text arrives first,
    tool_call_chunks only show up later in the same stream."""
    llm = _ScriptedLLM(
        [
            AIMessageChunk(content="The five rules are"),
            AIMessageChunk(content=" as follows..."),
            _tool_call_chunk('{"eligible', first=True),
            _tool_call_chunk('_only": true}', first=False),
        ]
    )
    sink = _SinkRecorder()

    result = await _stream_agent_response(llm, [], {}, sink)

    types = [e["type"] for e in sink.events]
    assert "answer_retract" not in types, (
        "retract is gone from the protocol -- pre-tool content is a kept "
        f"answer segment; got: {sink.events}"
    )
    # Both content chunks streamed, then the segment was closed the moment
    # the stream revealed itself as a tool-call turn.
    assert types == ["answer_delta", "answer_delta", "answer_done"]
    assert "".join(
        e["content"] for e in sink.events if e["type"] == "answer_delta"
    ) == "The five rules are as follows..."

    # The returned message still carries the accumulated tool call, so the
    # graph proceeds to tools_node exactly as before.
    assert result.tool_calls and result.tool_calls[0]["name"] == "build_filter_sql"
    assert result.tool_calls[0]["args"] == {"eligible_only": True}


@pytest.mark.asyncio
async def test_pure_tool_call_stream_emits_no_answer_events():
    llm = _ScriptedLLM(
        [
            _tool_call_chunk('{"eligible', first=True),
            _tool_call_chunk('_only": true}', first=False),
        ]
    )
    sink = _SinkRecorder()

    result = await _stream_agent_response(llm, [], {}, sink)

    assert sink.events == []
    assert result.tool_calls and result.tool_calls[0]["name"] == "build_filter_sql"


@pytest.mark.asyncio
async def test_pure_answer_stream_emits_deltas_then_done():
    llm = _ScriptedLLM(
        [
            AIMessageChunk(content="Here are"),
            AIMessageChunk(content=" the results."),
        ]
    )
    sink = _SinkRecorder()

    result = await _stream_agent_response(llm, [], {}, sink)

    types = [e["type"] for e in sink.events]
    assert types == ["answer_delta", "answer_delta", "answer_done"]
    assert not result.tool_calls


def test_turn_answer_text_joins_pre_tool_and_post_tool_segments():
    messages = [
        HumanMessage(content="Explain the rules, then list the top ASINs."),
        AIMessage(
            content="The five rules are as follows...",
            tool_calls=[
                {"name": "build_filter_sql", "args": {"eligible_only": True}, "id": "call_1"}
            ],
        ),
        ToolMessage(content='{"rows": []}', tool_call_id="call_1", name="build_filter_sql"),
        AIMessage(content="Here are the top ASINs: ..."),
    ]
    assert turn_answer_text(messages) == (
        "The five rules are as follows...\n\nHere are the top ASINs: ..."
    )


def test_turn_answer_text_only_covers_the_current_turn():
    messages = [
        HumanMessage(content="earlier question"),
        AIMessage(content="earlier answer -- must NOT leak into this turn"),
        HumanMessage(content="current question"),
        AIMessage(content="current answer"),
    ]
    assert turn_answer_text(messages) == "current answer"
