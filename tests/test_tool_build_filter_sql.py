"""HARNESS.md §7.1's build_filter_sql row: whitelist enforcement (unknown
fields ignored, not passed through / not erroring) + parametrized SQL (no
string interpolation) + the actual filter/sort/limit behavior against real
data.

Calls the tool's pure implementation functions directly (`build_filter_select`
/ `build_filter_sql_impl`), bypassing the LLM entirely, per HARNESS.md §7.1's
"直接调工具函数" instruction. Uses `db_session` (tests/conftest.py's
per-test-transaction-rolled-back fixture) with a handful of hand-seeded
`Asin` rows so expectations don't depend on whatever happens to be in the
dev DB.
"""
import pytest
from sqlalchemy import select

from app.agent.tools import build_filter_select, build_filter_sql_impl
from app.models.asin import Asin

pytestmark = pytest.mark.asyncio


async def _seed(db_session, rows: list[dict]) -> None:
    for row in rows:
        db_session.add(Asin(**row))
    await db_session.flush()


_FIXTURE_ROWS = [
    dict(asin="B0000000A1", title="High ROI eligible", eligible=True, computed_roi_pct=100.0, amazon_buybox_pct=10.0, supplier_cost=5.0),
    dict(asin="B0000000A2", title="Mid ROI eligible", eligible=True, computed_roi_pct=30.0, amazon_buybox_pct=50.0, supplier_cost=15.0),
    dict(asin="B0000000A3", title="Low ROI eligible", eligible=True, computed_roi_pct=5.0, amazon_buybox_pct=90.0, supplier_cost=40.0),
    dict(asin="B0000000A4", title="High ROI ineligible", eligible=False, computed_roi_pct=200.0, amazon_buybox_pct=5.0, supplier_cost=2.0),
]


# --- whitelist enforcement (pure, no DB) --------------------------------


def test_unknown_kwarg_is_silently_ignored_not_passed_through():
    stmt, applied = build_filter_select(min_roi=25, some_bogus_field="drop table asins")
    assert "some_bogus_field" not in applied
    assert applied["min_roi"] == 25
    # The bogus field never reaches the compiled SQL text at all.
    assert "bogus" not in str(stmt).lower()
    assert "drop" not in str(stmt).lower()


def test_unrecognized_sort_falls_back_to_default_instead_of_passthrough():
    stmt, applied = build_filter_select(sort="'; DROP TABLE asins; --")
    assert applied["sort"] == "roi_desc"  # whitelist default, not the raw string
    assert "DROP" not in str(stmt)


def test_values_are_bound_parameters_not_string_interpolated():
    stmt, _ = build_filter_select(min_roi=25.5, max_supplier_cost=19.99)
    compiled = str(stmt)
    # SQLAlchemy renders bound params as placeholders (e.g. `:computed_roi_pct_1`),
    # never inlines the literal value into the SQL text.
    assert "25.5" not in compiled
    assert "19.99" not in compiled
    assert ":" in compiled  # some bind-param placeholder is present


# --- end-to-end against real (seeded) data ------------------------------


async def test_eligible_only_and_min_roi_filter(db_session):
    await _seed(db_session, _FIXTURE_ROWS)
    result = await build_filter_sql_impl(db_session, eligible_only=True, min_roi=10)
    asins = {row["asin"] for row in result["rows"]}
    assert asins == {"B0000000A1", "B0000000A2"}
    assert result["active_filters"]["eligible_only"] is True
    assert result["active_filters"]["min_roi"] == 10
    assert result["last_result_asins"] == [row["asin"] for row in result["rows"]]


async def test_max_amazon_pct_and_max_supplier_cost(db_session):
    await _seed(db_session, _FIXTURE_ROWS)
    result = await build_filter_sql_impl(
        db_session, max_amazon_pct=60, max_supplier_cost=20
    )
    asins = {row["asin"] for row in result["rows"]}
    # A1 (10%, $5), A2 (50%, $15) pass both; A3 fails amazon_pct; A4 fails supplier_cost budget? (2<=20 but amazon 5<=60 -> actually A4 passes both filters too)
    assert "B0000000A3" not in asins  # amazon_buybox_pct 90 > 60
    assert "B0000000A1" in asins
    assert "B0000000A2" in asins


async def test_default_sort_is_roi_desc_and_limit_applies(db_session):
    await _seed(db_session, _FIXTURE_ROWS)
    result = await build_filter_sql_impl(db_session, limit=2)
    roi_values = [row["computed_roi_pct"] for row in result["rows"]]
    assert roi_values == sorted(roi_values, reverse=True)
    assert len(result["rows"]) == 2
    assert result["active_filters"]["limit"] == 2


async def test_excluded_asins_are_hard_filtered_regardless_of_llm_args(db_session):
    """excluded_asins (from the user's durable preferences) is applied at
    the code layer, not surfaced as an LLM-controllable field -- see
    app/agent/tools.py's build_filter_select comment."""
    await _seed(db_session, _FIXTURE_ROWS)
    result = await build_filter_sql_impl(
        db_session, eligible_only=True, excluded_asins=["B0000000A1"]
    )
    asins = {row["asin"] for row in result["rows"]}
    assert "B0000000A1" not in asins
    assert "excluded_asins" not in result["active_filters"]  # not user-visible state


async def test_unknown_kwarg_ignored_end_to_end(db_session):
    await _seed(db_session, _FIXTURE_ROWS)
    # Should not raise even though `not_a_real_filter` isn't whitelisted.
    result = await build_filter_sql_impl(db_session, min_roi=0, not_a_real_filter=123)
    assert "not_a_real_filter" not in result["active_filters"]
