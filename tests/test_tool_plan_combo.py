"""HARNESS.md §7.1's plan_combo row: stays within budget, excludes the
user's `excluded_asins`, category_count > 1 when diversify_categories is
requested, and -- the big one -- byte-for-byte determinism across repeated
runs with the same input (no randomness, no hidden LLM arithmetic).

`plan_combo_from_candidates` is pure (no DB) and takes an explicit
candidate list, which is what most of these tests use so the fixture is
fully under the test's control. `plan_combo_impl` covers the DB
round-trip (querying eligible ASINs, excluding a preference-list ASIN) on
top of it.
"""
import pytest

from app.agent.tools import infer_category, plan_combo_from_candidates, plan_combo_impl
from app.models.asin import Asin

pytestmark = pytest.mark.asyncio


def _candidate(asin, cost, roi, category):
    return {
        "asin": asin,
        "title": f"Item {asin}",
        "supplier_cost": cost,
        "computed_roi_pct": roi,
        "category": category,
    }


_CANDIDATES = [
    _candidate("B01", 10.0, 100.0, "electrical"),
    _candidate("B02", 20.0, 90.0, "kitchen"),
    _candidate("B03", 30.0, 80.0, "tools"),
    _candidate("B04", 5.0, 70.0, "electrical"),
    _candidate("B05", 50.0, 60.0, "home"),
]


# --- plan_combo_from_candidates (pure) ----------------------------------


def test_total_spend_never_exceeds_budget():
    plan = plan_combo_from_candidates(_CANDIDATES, budget=500)
    assert plan["total_spent"] <= 500
    assert sum(item["subtotal"] for item in plan["items"]) == pytest.approx(
        plan["total_spent"]
    )


def test_diversify_categories_yields_more_than_one_category():
    plan = plan_combo_from_candidates(_CANDIDATES, budget=100, diversify_categories=True)
    assert plan["category_count"] > 1


def test_without_diversify_may_concentrate_in_one_category():
    # Budget only affords B04 ($5, electrical) -- diversify off, so the
    # greedy fill has no reason to touch any other category.
    plan = plan_combo_from_candidates(_CANDIDATES, budget=6, diversify_categories=False)
    assert plan["category_count"] == 1
    assert {item["asin"] for item in plan["items"]} == {"B04"}


def test_determinism_same_input_twice_is_byte_for_byte_identical():
    plan_1 = plan_combo_from_candidates(_CANDIDATES, budget=137.42, diversify_categories=True)
    plan_2 = plan_combo_from_candidates(_CANDIDATES, budget=137.42, diversify_categories=True)
    assert plan_1 == plan_2


def test_determinism_holds_without_diversify_too():
    plan_1 = plan_combo_from_candidates(_CANDIDATES, budget=63.0)
    plan_2 = plan_combo_from_candidates(_CANDIDATES, budget=63.0)
    assert plan_1 == plan_2


def test_zero_or_negative_budget_yields_empty_plan_not_a_crash():
    plan = plan_combo_from_candidates(_CANDIDATES, budget=0)
    assert plan["items"] == []
    plan_neg = plan_combo_from_candidates(_CANDIDATES, budget=-50)
    assert plan_neg["items"] == []


def test_infer_category_is_deterministic_across_calls():
    assert infer_category("B01", "Some Title") == infer_category("B01", "Some Title")


# --- plan_combo_impl (DB round-trip) ------------------------------------


async def test_plan_combo_impl_excludes_preference_asins(db_session):
    db_session.add_all(
        [
            Asin(asin="B0100000A1", title="A", eligible=True, computed_roi_pct=100.0, supplier_cost=10.0),
            Asin(asin="B0100000A2", title="B", eligible=True, computed_roi_pct=90.0, supplier_cost=20.0),
        ]
    )
    await db_session.flush()

    plan = await plan_combo_impl(
        db_session, budget=500, excluded_asins=["B0100000A1"]
    )
    asins = {item["asin"] for item in plan["items"]}
    assert "B0100000A1" not in asins
    assert "B0100000A2" in asins


async def test_plan_combo_impl_only_considers_eligible_asins(db_session):
    db_session.add_all(
        [
            Asin(asin="B0200000A1", title="Eligible", eligible=True, computed_roi_pct=100.0, supplier_cost=10.0),
            Asin(asin="B0200000A2", title="Ineligible", eligible=False, computed_roi_pct=500.0, supplier_cost=1.0),
        ]
    )
    await db_session.flush()

    plan = await plan_combo_impl(db_session, budget=500)
    asins = {item["asin"] for item in plan["items"]}
    assert "B0200000A2" not in asins
