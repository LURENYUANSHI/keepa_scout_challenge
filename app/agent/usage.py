"""Token usage collection -> `llm_usage_log`. See ARCHITECTURE.md §4.4/§9.

**Deviation from ARCHITECTURE.md §4.4, flagged explicitly**: that section
says to use `get_usage_metadata_callback()` (LangChain's built-in local
token-usage aggregator). That function -- and its sibling
`UsageMetadataCallbackHandler` -- don't exist in `langchain-core==0.2.43`,
which is what this repo's pinned `requirements.txt` (`langchain-openai>=0.1,
<0.2`) actually resolves to (verified: `python -c "from
langchain_core.callbacks.usage import get_usage_metadata_callback"` raises
`ModuleNotFoundError` in the installed environment; that helper landed in a
later `langchain-core` than this repo pins). Rather than bump the pin (a
change with a wider blast radius than this phase's scope), `TokenUsageCollector`
below is a ~15-line hand-rolled `AsyncCallbackHandler` that reads the same
underlying data those helpers would: `ChatOpenAI` already populates
`response.llm_output["token_usage"]` (and `AIMessage.usage_metadata`) from
the OpenAI-compatible response's `usage` field on every call in this
version (verified against a real DeepSeek call, not assumed) -- so the
numbers are identical, just collected by hand instead of by an official
helper.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage import LlmUsageLog


class TokenUsageCollector(AsyncCallbackHandler):
    """Pass one instance per top-level `graph.ainvoke()` (or per `/ask`
    request) via `config={"callbacks": [collector]}` -- it aggregates
    across every LLM call that happens during that invocation (a `/chat`
    turn can trigger 1-N calls across tool-calling rounds; `/ask` makes
    exactly 1-2)."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.call_count = 0

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        usage = (response.llm_output or {}).get("token_usage") or {}
        if not usage:
            return
        self.input_tokens += usage.get("prompt_tokens", 0) or 0
        self.output_tokens += usage.get("completion_tokens", 0) or 0
        self.total_tokens += usage.get("total_tokens", 0) or 0
        self.call_count += 1


async def log_usage(
    session: AsyncSession,
    *,
    user_id: Any,
    session_id: str | None,
    endpoint: str,
    model: str,
    collector: TokenUsageCollector,
) -> None:
    """Writes one aggregated `llm_usage_log` row for this request/turn.
    Always writes (even if `collector.call_count == 0`, e.g. an
    out-of-scope refusal that never called the LLM at all shouldn't happen
    in practice, but a zero row is still cheap, honest bookkeeping rather
    than silently skipping)."""
    session.add(
        LlmUsageLog(
            user_id=user_id,
            session_id=session_id,
            endpoint=endpoint,
            model=model,
            input_tokens=collector.input_tokens,
            output_tokens=collector.output_tokens,
            total_tokens=collector.total_tokens,
        )
    )
    await session.commit()
