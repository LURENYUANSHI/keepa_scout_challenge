"""Shared `ChatOpenAI` construction — the one place LLM clients get built
from, so the base_url/api_key/model wiring to `settings` exists in exactly
one place. app/agent/graph.py (the `/chat` agent, tool-bound + streaming)
is the sole remaining caller since the `/ask` endpoint's removal; kept as
its own module anyway -- the provider-wiring seam is where the next
consumer (a batch summarizer, an eval harness) would plug in.
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
