"""The 6 tools from ARCHITECTURE.md §4.2 / HARNESS.md §7.1.

Design note -- why this file has two layers per tool, not one
(read this before the individual tool sections below):

Every tool here is split into:
  1. A **pure implementation function** (`*_impl` / helpers like
     `resolve_reference`, `plan_combo_from_candidates`,
     `validate_readonly_sql`) that takes plain arguments (a DB session, a
     `dict` of filters, a `last_result_asins` list, ...) and returns a
     plain `dict`. These have no LangChain/LangGraph dependency at all --
     HARNESS.md §7.1 wants tests that "直接调工具函数" (call the tool
     functions directly, bypassing the LLM entirely), and importing/calling
     a plain async function with explicit arguments is the simplest way to
     do that. `tests/test_tool_*.py` import these.
  2. A **schema-only `@tool`-decorated stub** (the `ALL_TOOLS` list at the
     bottom) whose only job is to give `ChatOpenAI.bind_tools()` a name +
     docstring + JSON Schema to show the model. These stubs are never
     actually invoked -- see app/agent/graph.py's `tools_node` docstring
     for why (short version: the installed langchain-core==0.2.43 doesn't
     have `InjectedToolCallId`, so a LangChain `@tool` function has no
     built-in way to learn its own `tool_call_id` and therefore can't
     return a `Command` that updates non-`messages` state fields the way
     newer langgraph tutorials show -- verified by reading the installed
     package's source, not assumed from newer docs). `graph.py`'s
     hand-rolled tools node dispatches by tool name straight to the `*_impl`
     functions below and builds the `ToolMessage`/state update itself.

Each whitelisted-args impl function also takes `**_ignored: Any` --
HARNESS.md §7.1's `build_filter_sql` row explicitly wants "未知字段被忽略
且不报错" (unknown fields silently ignored, not an error) when called
directly with a stray extra key, independent of whatever Pydantic-level
filtering `graph.py` also does before dispatch.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError, ProgrammingError

from langchain_core.tools import tool
from langgraph.store.base import BaseStore

from app.ingest import PRICE_ANOMALY_THRESHOLD_PCT, is_price_anomaly
from app.models.asin import Asin, AsinPriceStats

# =========================================================================
# Shared helpers
# =========================================================================

# Mirrors app/routers/eligibility.py's STALE_AFTER_HOURS -- duplicated
# rather than imported to keep app/agent/ decoupled from app/routers/
# (routers depend on the agent, not the other way around); both are the
# same HARNESS.md §5 constant.
STALE_AFTER_HOURS = 24


def _to_float(value: Any) -> float | None:
    """Postgres NUMERIC columns come back as `Decimal` via SQLAlchemy --
    normalize to `float` for JSON tool output / arithmetic."""
    return float(value) if value is not None else None


def _asin_summary(row: Asin) -> dict[str, Any]:
    """The compact per-ASIN shape used in build_filter_sql/run_readonly_sql
    result lists -- enough for the LLM to cite specifics without dumping
    every column."""
    return {
        "asin": row.asin,
        "title": row.title,
        "eligible": row.eligible,
        "filter_failed": row.filter_failed,
        "computed_roi_pct": _to_float(row.computed_roi_pct),
        "buybox": _to_float(row.buybox),
        "amazon_buybox_pct": _to_float(row.amazon_buybox_pct),
        "supplier_cost": _to_float(row.supplier_cost),
        "sales_rank": row.sales_rank,
        "monthly_sold": _to_float(row.monthly_sold),
        "snapshot_at": row.snapshot_at.isoformat() if row.snapshot_at else None,
    }


def _asin_detail(row: Asin, stats: AsinPriceStats | None) -> dict[str, Any]:
    """The fuller shape used by lookup_asin -- includes 90-day stats,
    freshness, and price-anomaly notes (HARNESS.md §5)."""
    detail = _asin_summary(row)

    from datetime import datetime, timezone

    if row.snapshot_at is not None:
        age_hours = (
            datetime.now(timezone.utc) - row.snapshot_at
        ).total_seconds() / 3600
        if age_hours > STALE_AFTER_HOURS:
            detail["data_freshness_note"] = (
                f"data last refreshed {int(age_hours)}h ago — consider POST /refresh"
            )

    if stats is not None:
        avg_90d = _to_float(stats.avg_90d)
        detail["avg_90d"] = avg_90d
        detail["min_90d"] = _to_float(stats.min_90d)
        detail["current_deviation_pct"] = _to_float(stats.current_deviation_pct)
        current_buybox = _to_float(row.buybox)
        if is_price_anomaly(current_buybox, avg_90d):
            deviation_pct = round(100 * (current_buybox - avg_90d) / avg_90d, 1)
            detail["price_anomaly_note"] = (
                f"buybox (${current_buybox}) deviates {deviation_pct}% from the "
                f"90-day average (${avg_90d}) — possible price anomaly "
                f"(threshold: {PRICE_ANOMALY_THRESHOLD_PCT}%)"
            )

    return detail


# =========================================================================
# 1. build_filter_sql
# =========================================================================

# Whitelisted sort keys -- ARCHITECTURE.md §4.2: "只认白名单字段". Anything
# else (including an attempted raw `ORDER BY` string) falls back to the
# default below instead of being passed through.
_SORT_COLUMNS: dict[str, tuple[Any, str]] = {
    "roi_desc": (Asin.computed_roi_pct, "desc"),
    "roi_asc": (Asin.computed_roi_pct, "asc"),
    "amazon_pct_asc": (Asin.amazon_buybox_pct, "asc"),
    "amazon_pct_desc": (Asin.amazon_buybox_pct, "desc"),
    "supplier_cost_asc": (Asin.supplier_cost, "asc"),
    "supplier_cost_desc": (Asin.supplier_cost, "desc"),
}
_DEFAULT_SORT = "roi_desc"
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def build_filter_select(
    *,
    min_roi: float | None = None,
    eligible_only: bool | None = None,
    max_amazon_pct: float | None = None,
    max_supplier_cost: float | None = None,
    sort: str | None = None,
    limit: int | None = None,
    excluded_asins: list[str] | None = None,
    **_ignored: Any,
) -> tuple[Select, dict[str, Any]]:
    """Pure SQL-building step -- no DB access, no I/O. Builds a parametrized
    SQLAlchemy Core `Select` (values are bound params, never string-
    interpolated -- HARNESS.md §7.1: "SQL 值一律走参数化,不做字符串拼接")
    from a whitelist of fields, and returns the normalized filter dict
    that becomes `active_filters`. `**_ignored` swallows anything not in
    the whitelist (e.g. a stray `sql` or `where` kwarg) silently.

    Split out from `build_filter_sql_impl` (which adds the DB round-trip)
    so tests can inspect the generated statement/params without a database.
    """
    stmt = select(Asin)
    applied: dict[str, Any] = {}

    # `excluded_asins` (user's durable exclusion list, from the Store) is
    # deliberately NOT part of `applied`/`active_filters` -- it's a
    # code-level guarantee applied on every call regardless of what the LLM
    # passes, not a user-visible/LLM-controlled filter field (HARNESS.md
    # §7.2 scenario E/F: "该 session 的所有后续推荐都必须排除它", enforced
    # in SQL rather than left to the model remembering to mention it).
    if excluded_asins:
        stmt = stmt.where(Asin.asin.not_in(list(excluded_asins)))

    if eligible_only:
        stmt = stmt.where(Asin.eligible.is_(True))
        applied["eligible_only"] = True

    if min_roi is not None:
        stmt = stmt.where(Asin.computed_roi_pct >= min_roi)
        applied["min_roi"] = min_roi

    if max_amazon_pct is not None:
        stmt = stmt.where(Asin.amazon_buybox_pct <= max_amazon_pct)
        applied["max_amazon_pct"] = max_amazon_pct

    if max_supplier_cost is not None:
        stmt = stmt.where(Asin.supplier_cost <= max_supplier_cost)
        applied["max_supplier_cost"] = max_supplier_cost

    sort_key = sort if sort in _SORT_COLUMNS else _DEFAULT_SORT
    column, direction = _SORT_COLUMNS[sort_key]
    stmt = stmt.order_by(
        (column.desc() if direction == "desc" else column.asc()).nulls_last(),
        Asin.asin.asc(),  # deterministic tie-break
    )
    applied["sort"] = sort_key

    try:
        lim = int(limit) if limit else _DEFAULT_LIMIT
    except (TypeError, ValueError):
        lim = _DEFAULT_LIMIT
    lim = max(1, min(lim, _MAX_LIMIT))
    stmt = stmt.limit(lim)
    applied["limit"] = lim

    return stmt, applied


async def build_filter_sql_impl(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    """Execute the filter built by `build_filter_select` against the real
    `asins` table and return the matching rows + the resulting
    `active_filters`/`last_result_asins`."""
    stmt, applied = build_filter_select(**kwargs)
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    return {
        "active_filters": applied,
        "last_result_asins": [r.asin for r in rows],
        "rows": [_asin_summary(r) for r in rows],
        "row_count": len(rows),
    }


# =========================================================================
# 2. lookup_asin
# =========================================================================


def resolve_reference(
    *,
    last_result_asins: list[str] | None,
    resolved_entity: str | None,
    asin: str | None = None,
    reference: dict | None = None,
) -> dict[str, Any]:
    """Pure reference resolution -- explicit `asin`, or an `{ordinal,
    pronoun}` reference against `last_result_asins`/`resolved_entity`.
    Returns `{"asin": "..."}` on success or `{"error": "..."}` — never
    raises, and an out-of-range ordinal is always the latter, never an
    `IndexError` (HARNESS.md §7.1's `lookup_asin` row).

    Ordinal is 1-based ("the second one" -> ordinal=2 -> index 1).
    """
    if asin:
        return {"asin": asin.strip().upper()}

    last_result_asins = last_result_asins or []
    reference = reference or {}
    ordinal = reference.get("ordinal")
    pronoun = reference.get("pronoun")

    if ordinal is not None:
        try:
            ordinal_int = int(ordinal)
        except (TypeError, ValueError):
            return {"error": f"Invalid ordinal: {ordinal!r}."}
        index = ordinal_int - 1
        if ordinal_int < 1 or index >= len(last_result_asins):
            return {
                "error": (
                    f"Ordinal {ordinal_int} is out of range — the last result "
                    f"set only has {len(last_result_asins)} item(s)."
                )
            }
        return {"asin": last_result_asins[index]}

    if pronoun:
        if resolved_entity:
            return {"asin": resolved_entity}
        if len(last_result_asins) == 1:
            return {"asin": last_result_asins[0]}
        return {
            "error": (
                "No prior ASIN to resolve 'it'/'that' to — ask about a "
                "specific ASIN first."
            )
        }

    return {
        "error": "lookup_asin needs either an explicit asin or a reference "
        "(ordinal/pronoun)."
    }


async def lookup_asin_impl(
    session: AsyncSession,
    *,
    last_result_asins: list[str] | None = None,
    resolved_entity: str | None = None,
    asin: str | None = None,
    reference: dict | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    resolved = resolve_reference(
        last_result_asins=last_result_asins,
        resolved_entity=resolved_entity,
        asin=asin,
        reference=reference,
    )
    if "error" in resolved:
        return resolved

    target = resolved["asin"]
    row = await session.get(Asin, target)
    if row is None:
        return {"error": f"ASIN {target!r} not found in catalog."}
    stats = await session.get(AsinPriceStats, target)
    return {"resolved_entity": target, "detail": _asin_detail(row, stats)}


# =========================================================================
# 3. plan_combo
# =========================================================================

# CHALLENGE.md's chat scenario G / HARNESS.md §7.1's plan_combo row want
# "跨类目" (cross-category) diversification, but app/models/asin.py (built
# in an earlier phase, before /chat existed) has no `category` column --
# Keepa's product object doesn't hand back a clean single "category" field
# either without extra API calls we don't otherwise need. This is a real,
# known gap (see REPORT.md's "故意没做好的地方"): categories here are a
# deterministic hash-bucket over (title or asin), NOT real Amazon browse
# categories. It's good enough to prove the diversification *algorithm*
# works and stays deterministic, but the category labels themselves are
# fake and should not be read as real merchandising categories.
_FAKE_CATEGORIES = (
    "electrical",
    "kitchen",
    "tools",
    "home",
    "toys",
    "office",
    "outdoor",
    "beauty",
)


def infer_category(asin: str, title: str | None) -> str:
    """Deterministic pseudo-category: sha256(title or asin) -> bucket.
    Same input always maps to the same category (required for the
    determinism acceptance criterion), but see the module comment above --
    this is a documented stand-in, not real category data.
    """
    basis = (title or asin).strip().lower()
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(_FAKE_CATEGORIES)
    return _FAKE_CATEGORIES[idx]


def plan_combo_from_candidates(
    candidates: list[dict[str, Any]],
    *,
    budget: float,
    diversify_categories: bool = False,
) -> dict[str, Any]:
    """Deterministic greedy combo planner -- no LLM arithmetic, no
    randomness. `candidates` must already be in a deterministic order
    (ROI desc, ASIN asc tie-break) -- see `plan_combo_impl` below for how
    that's produced from the DB.

    Phase 1 (only if diversify_categories): buy exactly 1 unit of the
    highest-ROI candidate in each distinct category, in the candidates'
    existing deterministic order, while budget allows -- guarantees
    category_count > 1 whenever >= 2 categories are affordable.
    Phase 2: greedily fill remaining budget by ROI, buying as many units of
    each candidate (in order) as fit, accumulating onto anything already
    picked in phase 1.

    Same `candidates` + `budget` + `diversify_categories` in -> byte-for-
    byte identical output out, every time (HARNESS.md §7.1: "确定性——同样
    输入跑两次结果完全一致").
    """
    try:
        remaining = Decimal(str(budget))
    except InvalidOperation:
        remaining = Decimal("0")
    if remaining < 0:
        remaining = Decimal("0")

    picked: dict[str, dict[str, Any]] = {}
    used_categories: set[str] = set()

    def _add(item: dict[str, Any], qty: int) -> None:
        nonlocal remaining
        cost = Decimal(str(item["supplier_cost"]))
        subtotal = (cost * qty).quantize(Decimal("0.01"))
        existing = picked.get(item["asin"])
        if existing:
            existing["qty"] += qty
            existing["subtotal"] = float(
                (Decimal(str(existing["subtotal"])) + subtotal).quantize(Decimal("0.01"))
            )
        else:
            picked[item["asin"]] = {
                "asin": item["asin"],
                "title": item.get("title"),
                "category": item["category"],
                "unit_cost": float(cost),
                "computed_roi_pct": item.get("computed_roi_pct"),
                "qty": qty,
                "subtotal": float(subtotal),
            }
        remaining -= subtotal
        used_categories.add(item["category"])

    if diversify_categories:
        seen_categories: set[str] = set()
        for item in candidates:
            category = item["category"]
            if category in seen_categories:
                continue
            cost = Decimal(str(item["supplier_cost"]))
            if cost > 0 and cost <= remaining:
                _add(item, 1)
                seen_categories.add(category)

    for item in candidates:
        cost = Decimal(str(item["supplier_cost"]))
        if cost <= 0 or cost > remaining:
            continue
        qty = int(remaining // cost)
        if qty >= 1:
            _add(item, qty)

    items = list(picked.values())
    total_spent = round(sum(i["subtotal"] for i in items), 2)

    return {
        "items": items,
        "total_spent": total_spent,
        "budget": float(budget),
        "remaining_budget": round(float(remaining), 2),
        "categories_used": sorted(used_categories),
        "category_count": len(used_categories),
    }


async def plan_combo_impl(
    session: AsyncSession,
    *,
    budget: float,
    diversify_categories: bool = False,
    excluded_asins: list[str] | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    excluded = set(excluded_asins or [])

    result = await session.execute(select(Asin).where(Asin.eligible.is_(True)))
    rows = list(result.scalars().all())

    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row.asin in excluded:
            continue
        cost = _to_float(row.supplier_cost)
        if not cost or cost <= 0:
            continue
        roi = _to_float(row.computed_roi_pct)
        candidates.append(
            {
                "asin": row.asin,
                "title": row.title,
                "supplier_cost": cost,
                "computed_roi_pct": roi,
                "category": infer_category(row.asin, row.title),
            }
        )

    # Deterministic order: ROI desc (None sorts last), ASIN asc tie-break.
    candidates.sort(key=lambda c: (c["computed_roi_pct"] is None, -(c["computed_roi_pct"] or 0.0), c["asin"]))

    plan = plan_combo_from_candidates(
        candidates, budget=budget, diversify_categories=diversify_categories
    )
    plan["excluded_asins"] = sorted(excluded)
    return plan


# =========================================================================
# 4. run_readonly_sql
# =========================================================================

# ARCHITECTURE.md §4.2: run_readonly_sql is the ONLY tool that accepts raw
# SQL text -- this validator is the single choke point for that risk.
# HARNESS.md §7.1 explicitly wants this to catch keywords "even inside what
# looks like a comment" via a defensive blocklist match on the raw string,
# not a SQL parser that strips comments first. Word-boundary regex (rather
# than a bare substring check) is still "just blocklist-match the raw
# string" in spirit -- it's not parsing SQL structure -- but avoids a false
# positive on legitimate column names that happen to contain a keyword as a
# substring (e.g. `updated_at` contains "UPDATE"; `created_at` shows up a
# lot in this schema too). `\bDROP\b` still matches "DROP" inside a `--
# DROP TABLE` comment or mixed-case "DrOp" (case-insensitive), which is
# what actually matters here.
_FORBIDDEN_KEYWORDS = (
    "DROP",
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "MERGE",
    "CALL",
    "COPY",
    "VACUUM",
    "REINDEX",
    "ATTACH",
    "DETACH",
    "PRAGMA",
)
_MAX_ROWS = 500


def validate_readonly_sql(sql: str) -> str | None:
    """Returns an error message string if `sql` is unsafe to run, else
    `None`. Rules (HARNESS.md §7.1's run_readonly_sql row, verbatim):
      - single statement only (no `;`-separated second statement --  a
        single trailing `;` is tolerated)
      - must be a single `SELECT` (case-insensitive)
      - no DDL/DML keyword anywhere in the string, in any case, even
        inside a comment
    """
    if not sql or not sql.strip():
        return "Empty SQL."

    stripped = sql.strip()
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        return "Only a single SQL statement is allowed (no ';'-separated second statement)."

    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        return "Only SELECT statements are allowed."

    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", stripped, re.IGNORECASE):
            return f"Forbidden keyword detected: {keyword}."

    return None


async def run_readonly_sql_impl(session: AsyncSession, sql: str) -> dict[str, Any]:
    """Callers (app/agent/graph.py's tools_node) are
    expected to pass a session that hasn't run other statements in the same
    transaction yet where possible -- but this function doesn't assume it.

    An earlier version of this function additionally issued `SET
    TRANSACTION READ ONLY` before executing, as defense-in-depth on top of
    the validator above. That was dropped: Postgres requires `SET
    TRANSACTION` to be the very first statement in a transaction, which
    doesn't hold when this runs on a session that's already executed prior
    queries in the same request (`/chat`'s tools_node when
    `run_readonly_sql` isn't the first tool call in a multi-tool-call
    round) -- it would raise instead of protecting anything. The actual
    safety net is: (1) `validate_readonly_sql` above is SELECT-only /
    single-statement / DDL-DML-blocklisted, and (2) this function never
    calls `commit()` -- whatever session it's given, the caller is
    responsible for not committing it afterward with unrelated writes
    still pending (tools_node uses a session dedicated to read-only tool
    work, never the session that later commits `llm_usage_log`).
    """
    error = validate_readonly_sql(sql)
    if error:
        return {"error": error, "sql": sql}

    try:
        result = await session.execute(text(sql))
        rows = [dict(r._mapping) for r in result.fetchmany(_MAX_ROWS)]
    except (ProgrammingError, DBAPIError) as exc:
        return {"error": f"SQL execution failed: {exc}", "sql": sql}
    finally:
        await session.rollback()  # read-only query; nothing to commit, ever

    return {"sql": sql, "rows": rows, "row_count": len(rows)}


# =========================================================================
# 5. update_preferences
# =========================================================================

PREFERENCES_KEY = "prefs"


def _preferences_namespace(user_id: str) -> tuple[str, str]:
    return ("preferences", str(user_id))


async def get_preferences(store: BaseStore, user_id: str) -> dict[str, Any]:
    item = await store.aget(_preferences_namespace(user_id), PREFERENCES_KEY)
    if item is None:
        return {"budget_per_unit": None, "excluded_asins": [], "notes": []}
    return dict(item.value)


async def update_preferences_impl(
    store: BaseStore,
    user_id: str,
    *,
    budget_per_unit: float | None = None,
    exclude_asin: str | None = None,
    note: str | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """HARNESS.md §7.1's update_preferences row: `budget_per_unit` is
    REPLACE semantics (last call wins), `exclude_asin` is APPEND semantics
    (read-modify-write the existing list, never clobber it)."""
    namespace = _preferences_namespace(user_id)
    prefs = await get_preferences(store, user_id)

    if budget_per_unit is not None:
        prefs["budget_per_unit"] = float(budget_per_unit)  # replace

    if exclude_asin:
        asin = exclude_asin.strip().upper()
        excluded = list(prefs.get("excluded_asins", []))
        if asin not in excluded:
            excluded.append(asin)  # append, not overwrite
        prefs["excluded_asins"] = excluded

    if note:
        notes = list(prefs.get("notes", []))
        notes.append(note)
        prefs["notes"] = notes

    await store.aput(namespace, PREFERENCES_KEY, prefs)
    return prefs


# =========================================================================
# 6. reset_topic
# =========================================================================


def reset_topic_impl() -> dict[str, Any]:
    """No args, no DB, no Store access -- HARNESS.md §7.1: clears only the
    short-term graph-state fields, never touches the Store-backed
    preferences."""
    return {"active_filters": {}, "last_result_asins": [], "resolved_entity": None}


# =========================================================================
# LLM-facing tool schemas (bind_tools() only -- see module docstring)
# =========================================================================


@tool
def build_filter_sql(
    min_roi: Optional[float] = None,
    eligible_only: Optional[bool] = None,
    max_amazon_pct: Optional[float] = None,
    max_supplier_cost: Optional[float] = None,
    sort: Optional[
        Literal[
            "roi_desc",
            "roi_asc",
            "amazon_pct_asc",
            "amazon_pct_desc",
            "supplier_cost_asc",
            "supplier_cost_desc",
        ]
    ] = None,
    limit: Optional[int] = None,
) -> str:
    """Filter/sort/limit the ASIN catalog by structured criteria: minimum
    ROI percent, eligibility, maximum Amazon BuyBox share percent, maximum
    supplier cost, sort order, and result limit. Use this whenever the user
    wants to see, filter, sort, or limit a set of ASINs. Filters accumulate
    across turns -- a new call only needs to specify the fields that are
    changing; a new value for a field that was already set REPLACES the old
    one (it doesn't add to it). Returns matching ASINs."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


@tool
def lookup_asin(
    asin: Optional[str] = None,
    reference: Optional[dict] = None,
) -> str:
    """Look up full details for one ASIN: either an explicit `asin`, or a
    `reference` object resolving a pronoun or an ordinal position against
    the most recent result set, shaped as {"ordinal": <1-based int>} (e.g.
    "the second one" -> {"ordinal": 2}) or {"pronoun": true} (e.g. "it",
    "that ASIN"). Use this whenever the user refers to a specific ASIN by
    name, position, or pronoun instead of asking for a filtered list."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


@tool
def plan_combo(
    budget: float,
    diversify_categories: Optional[bool] = None,
) -> str:
    """Deterministically plan a purchase combo of eligible ASINs that fits
    within `budget`, optionally spread across multiple categories
    (diversify_categories=true). Excludes any ASIN the user has previously
    asked to exclude. Use this whenever the user describes a budget and
    asks for a combo/bundle/purchase plan rather than a simple filtered
    list."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


@tool
def run_readonly_sql(sql: str) -> str:
    """Run one read-only SQL SELECT statement directly against the `asins`
    / `asin_price_stats` tables for analysis that build_filter_sql's fixed
    whitelist can't express (aggregates, joins, custom comparisons, etc.).
    Must be a single SELECT statement -- no DDL/DML, no multiple
    statements. Prefer build_filter_sql for simple filter/sort/limit
    requests; use this for open-ended analytical questions."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


@tool
def update_preferences(
    budget_per_unit: Optional[float] = None,
    exclude_asin: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Persist a durable user preference that should apply to ALL future
    queries in ALL sessions for this user, not just this conversation:
    a per-unit budget cap (replaces any previous budget), an ASIN to
    permanently exclude from recommendations (adds to the exclusion list,
    doesn't replace it), and/or a free-text note. Use this whenever the
    user states a standing constraint ("my budget is $20") or a correction
    ("don't recommend B0XXXXXXX anymore")."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


@tool
def reset_topic() -> str:
    """Clear the current conversation's short-term filters, last result
    set, and resolved-entity reference (but NOT the user's durable
    preferences from update_preferences). Use this when the user explicitly
    changes topic / says to forget the current filters or results, e.g.
    "actually, forget that" / "let's start over"."""
    raise NotImplementedError("dispatched by app.agent.graph.tools_node")


ALL_TOOLS = [
    build_filter_sql,
    lookup_asin,
    plan_combo,
    run_readonly_sql,
    update_preferences,
    reset_topic,
]

TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
