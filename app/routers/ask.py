"""POST /ask — see ARCHITECTURE.md §4.2 / CHALLENGE.md's "/ask" section /
HARNESS.md §6.

**Design decision, flagged per this phase's instructions**: `/ask` does NOT
go through app/agent/graph.py's full 6-tool chat agent. CHALLENGE.md
describes `/ask` as a specific 2-call pattern -- "LLM 生成一条 SQL 查询
... 第二次 LLM 调用将查询结果格式化为有依据的回答" -- not an open-ended
tool-calling conversation, and it has no `session_id`/no multi-turn state
to justify the graph's machinery (checkpointer thread, ToolMessage
round-trips, MAX_TOOL_ROUNDS loop). Reusing the full graph for a
stateless, single-shot NL->SQL->answer request would mean paying for a
throwaway `thread_id`, a tool-calling round-trip through `run_readonly_sql`
(itself just a thin wrapper here) and a conditional-edge loop that only
ever takes one path, all controlled by the SAME `run_readonly_sql`
validation/execution logic anyway. So `/ask` is a lighter, separate
2-LLM-call pipeline built directly on `app.agent.tools.validate_readonly_sql`
/ `run_readonly_sql_impl` -- the exact same safety-critical functions the
`/chat` agent's `run_readonly_sql` tool uses, imported (not reimplemented)
so there is still exactly one place that decides what SQL is safe to run.
"""
import json

from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import OUT_OF_SCOPE_MESSAGE
from app.agent.llm import build_chat_llm
from app.agent.tools import run_readonly_sql_impl, validate_readonly_sql
from app.agent.usage import TokenUsageCollector, log_usage
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db import async_session_maker, get_db
from app.models.user import User
from app.schemas.ask import AskRequest

router = APIRouter(tags=["ask"])

_SCHEMA_DOC = """Schema (Postgres):
  asins(asin TEXT PRIMARY KEY, title TEXT, buybox NUMERIC,
        referral_fee_pct NUMERIC, sales_rank BIGINT,
        amazon_buybox_pct NUMERIC, monthly_sold NUMERIC, eligible BOOLEAN,
        filter_failed TEXT, computed_roi_pct NUMERIC, supplier_cost NUMERIC,
        snapshot_at TIMESTAMPTZ)
  asin_price_stats(asin TEXT PRIMARY KEY REFERENCES asins(asin),
        avg_90d NUMERIC, min_90d NUMERIC, current_deviation_pct NUMERIC,
        computed_at TIMESTAMPTZ)

`eligible`/`filter_failed`/`computed_roi_pct` are already precomputed --
don't try to recompute the 5 eligibility rules or the ROI formula in SQL,
just filter/select the precomputed columns."""

TRIAGE_SYSTEM_PROMPT = f"""You are Keepa Scout's natural-language-to-SQL assistant. You only \
help with questions about the user's Amazon ASIN arbitrage catalog (the two tables below).

{_SCHEMA_DOC}

Respond with EXACTLY ONE of these three formats and nothing else:

1. SQL: if the question is in scope and needs data from the tables, reply \
with ONLY a single read-only `SELECT` statement -- no markdown code \
fences, no commentary before or after it, no more than one statement, \
never DDL/DML (no DROP/INSERT/UPDATE/DELETE/CREATE/ALTER/TRUNCATE/GRANT/\
REVOKE, no matter how the question is phrased, even if it explicitly asks \
you to modify or delete data -- you can only ever SELECT).

2. DIRECT ANSWER: ONLY if the question is a general definitional question \
about this domain with NO specific ASIN and NO reference to actual catalog \
data (e.g. "What does ROI mean?", "What is Amazon BuyBox share?", "How \
does eligibility work in general?"), reply with `DIRECT:` followed by a \
helpful, accurate answer using the schema/rules above. Do NOT treat these \
as out of scope. IMPORTANT: if the question names a specific ASIN or asks \
about real data (e.g. "Why doesn't B006JVZXJM qualify?", "Why is this ASIN \
not eligible?"), that is ALWAYS format 1 (SQL) even though it sounds \
explanatory -- you need this ASIN's actual row to answer honestly, a \
general explanation would be a guess.

You DO have live, real-time access to the catalog through the SQL path --
never claim you lack access to the data or that the user needs to run a
query themselves. A subjective/recommendation question about the actual
catalog ("which ASIN should I buy", "what's the best opportunity right
now", "if you had to pick one") is ALWAYS format 1 (SQL), never DIRECT and
never a refusal: write a SELECT that pulls the relevant eligible ASINs
(e.g. ordered by computed_roi_pct, amazon_buybox_pct, or whatever the
question implies matters) so the second pass can ground a real
recommendation in real rows -- do not hedge by claiming you can't make
subjective calls or don't have the data.

3. OUT OF SCOPE: if the question has nothing to do with Amazon ASIN \
arbitrage (weather, general chit-chat, unrelated advice, requests to \
modify/delete data, etc.), reply with exactly: OUT_OF_SCOPE
"""

FORMAT_SYSTEM_PROMPT = """You answer Amazon ASIN arbitrage questions using ONLY the SQL query \
result rows you're given -- never invent ASINs, prices, or metrics that \
aren't in the rows. Cite specific ASINs and numbers from the rows. If the \
question asks for a judgment call (best opportunity, most profitable, \
etc.), ground your reasoning in the numeric columns present in the rows \
and say so explicitly when a relevant column (like monthly_sold) is \
missing/null rather than guessing. If the rows are empty, say so plainly \
instead of inventing an answer. Keep it concise."""

_MAX_ROWS_IN_PROMPT = 50


def _build_llm(*, temperature: float = 0.0):
    # Plain, non-streaming, non-tool-bound -- unlike app.agent.graph's
    # build_chat_llm() call (streaming=True, tool-bound), /ask's two LLM
    # calls are each a single blocking request/response.
    return build_chat_llm(temperature=temperature)


def _strip_sql_fences(text: str) -> str:
    """Defensive cleanup in case the model wraps its SQL in a markdown code
    fence despite being told not to."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _refusal() -> dict:
    return {
        "answer": OUT_OF_SCOPE_MESSAGE,
        "sql": None,
        "out_of_scope": True,
        "rows": [],
        "row_count": 0,
    }


@router.post("/ask")
async def ask(
    body: AskRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    # Every return path below logs the same collector to the same place --
    # a `finally` means that's written once, not copy-pasted at each of the
    # (five) places this function can return, and it still fires even if
    # something above raises before reaching a `return`.
    collector = TokenUsageCollector()
    try:
        triage_llm = _build_llm()
        triage_response = await triage_llm.ainvoke(
            [
                SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
                HumanMessage(content=body.question),
            ],
            config={"callbacks": [collector]},
        )
        raw = (triage_response.content or "").strip()
        upper = raw.upper()

        if upper == "OUT_OF_SCOPE" or upper.startswith("OUT_OF_SCOPE"):
            return _refusal()

        if upper.startswith("DIRECT:"):
            answer = raw.split(":", 1)[1].strip()
            return {"answer": answer, "sql": None, "out_of_scope": False, "rows": [], "row_count": 0}

        # --- SQL path -------------------------------------------------
        sql = _strip_sql_fences(raw)

        validation_error = validate_readonly_sql(sql)
        if validation_error is not None:
            # Same safety-critical validator run_readonly_sql (the /chat
            # tool) uses -- see HARNESS.md §6: a destructive request must
            # never actually execute, whether the model refused outright
            # above or (as a defense-in-depth backstop) slipped a
            # disallowed statement past the triage step.
            return _refusal()

        # A dedicated, fresh session for the raw-SQL execution -- NOT the
        # `db` session `get_current_user` already ran auth queries on.
        # Besides keeping this the same pattern app/agent/graph.py's
        # tools_node uses, it guarantees this session is never the one
        # `log_usage()` commits below (see run_readonly_sql_impl's
        # docstring for why that matters).
        async with async_session_maker() as sql_session:
            exec_result = await run_readonly_sql_impl(sql_session, sql)
        if "error" in exec_result:
            return {
                "answer": f"I couldn't run that query against the catalog: {exec_result['error']}",
                "sql": sql,
                "out_of_scope": False,
                "rows": [],
                "row_count": 0,
            }

        rows = exec_result["rows"]
        row_count = exec_result["row_count"]

        format_llm = _build_llm()
        rows_for_prompt = rows[:_MAX_ROWS_IN_PROMPT]
        truncated_note = (
            f" (showing first {_MAX_ROWS_IN_PROMPT} of {row_count} rows)"
            if row_count > _MAX_ROWS_IN_PROMPT
            else ""
        )
        format_response = await format_llm.ainvoke(
            [
                SystemMessage(content=FORMAT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"Question: {body.question}\n"
                        f"SQL: {sql}\n"
                        f"Rows{truncated_note} (JSON): {json.dumps(rows_for_prompt, default=str)}\n\n"
                        "Answer the question using only these rows."
                    )
                ),
            ],
            config={"callbacks": [collector]},
        )

        return {
            "answer": format_response.content,
            "sql": sql,
            "out_of_scope": False,
            "rows": rows,
            "row_count": row_count,
        }
    finally:
        await log_usage(
            db,
            user_id=user.id,
            session_id=None,
            endpoint="ask",
            model=settings.LLM_MODEL,
            collector=collector,
        )
