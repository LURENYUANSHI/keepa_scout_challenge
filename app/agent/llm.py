"""Shared `ChatOpenAI` construction — the one place both app/agent/graph.py
(the `/chat` agent, tool-bound + streaming) and app/routers/ask.py (the
`/ask` triage/format calls, plain + non-streaming) build their LLM client
from, so the base_url/api_key/model wiring to `settings` exists in exactly
one place. Pure construction, no behavior of either caller changed by this
extraction -- graph.py still gets a streaming+tool-bound client, ask.py
still gets a plain one, each just asks for what it needs.
"""
from langchain_openai import ChatOpenAI

from app.config import settings


def build_chat_llm(
    *, temperature: float = 0.0, streaming: bool = False, stream_usage: bool = False
) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        model=settings.LLM_MODEL,
        temperature=temperature,
        streaming=streaming,
        stream_usage=stream_usage,
    )
