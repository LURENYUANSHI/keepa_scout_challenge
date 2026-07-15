"""Unit tests for app/eligibility.py — pure arithmetic, no DB/network needed.

Covers:
  - compute_payout / compute_roi against hand-computed fixtures (>=3 each).
  - check_eligibility: fully-eligible case, one case per rule engineered to
    fail ONLY that rule (so filter_failed is unambiguous), and the
    rank/monthly_sold demand-exemption interaction.
"""
from app.eligibility import check_eligibility, compute_payout, compute_roi


# --- compute_payout / compute_roi --------------------------------------


def test_compute_payout_example_1():
    # buybox=29.99, referral=15%, fba_pick_pack=300 cents ($3.00)
    # referral = 29.99 * 0.15 = 4.4985
    # fba = 3.00
    # storage = 0.50
    # payout = 29.99 - 4.4985 - 3.00 - 0.50 = 21.9915
    payout = compute_payout(29.99, 15, 300)
    assert payout == 29.99 - (29.99 * 0.15) - 3.00 - 0.50
    assert round(payout, 4) == 21.9915


def test_compute_payout_example_2():
    # buybox=100, referral=8%, fba_pick_pack=250 cents ($2.50)
    # referral = 100 * 0.08 = 8.0
    # fba = 2.50, storage = 0.50
    # payout = 100 - 8.0 - 2.50 - 0.50 = 89.0
    payout = compute_payout(100, 8, 250)
    assert payout == 89.0


def test_compute_payout_example_3():
    # buybox=50.5, referral=12%, fba_pick_pack=175 cents ($1.75)
    # referral = 50.5 * 0.12 = 6.06
    # fba = 1.75, storage = 0.50
    # payout = 50.5 - 6.06 - 1.75 - 0.50 = 42.19
    payout = compute_payout(50.5, 12, 175)
    assert round(payout, 4) == 42.19


def test_compute_roi_example_1():
    # From test_compute_payout_example_1: payout = 21.9915
    # supplier_cost=9.27, n_items=1 -> cost = 9.27
    # roi = 100 * (21.9915 - 9.27) / 9.27
    roi = compute_roi(29.99, 15, 300, 9.27, 1)
    expected = 100 * (21.9915 - 9.27) / 9.27
    assert round(roi, 6) == round(expected, 6)


def test_compute_roi_example_2():
    # From test_compute_payout_example_2: payout = 89.0
    # supplier_cost=10, n_items=2 -> cost = 20
    # roi = 100 * (89.0 - 20) / 20 = 345.0
    roi = compute_roi(100, 8, 250, 10, 2)
    assert roi == 345.0


def test_compute_roi_example_3():
    # From test_compute_payout_example_3: payout = 42.19
    # supplier_cost=20.5, n_items=None -> max(None or 1, 1) = 1 -> cost=20.5
    # roi = 100 * (42.19 - 20.5) / 20.5
    roi = compute_roi(50.5, 12, 175, 20.5, None)
    expected = 100 * (42.19 - 20.5) / 20.5
    assert round(roi, 6) == round(expected, 6)


def test_compute_roi_zero_cost_returns_none():
    # supplier_cost * max(n_items or 1, 1) <= 0 -> None
    assert compute_roi(29.99, 15, 300, 0, 1) is None
    assert compute_roi(29.99, 15, 300, -5, 1) is None


def test_compute_roi_n_items_zero_treated_as_one():
    # max(n_items or 1, 1): n_items=0 is falsy -> `0 or 1` -> 1
    # payout (from example 2) = 89.0; cost = 10 * 1 = 10
    # roi = 100 * (89.0 - 10) / 10 = 790.0
    roi_zero = compute_roi(100, 8, 250, 10, 0)
    roi_none = compute_roi(100, 8, 250, 10, None)
    assert roi_zero == roi_none == 790.0


# --- check_eligibility ---------------------------------------------------


def _base_eligible_fixture() -> dict:
    """A fixture engineered to pass all 5 rules."""
    return {
        "referral_fee_pct": 15,
        "sales_rank": 88_003,
        "monthly_sold": None,
        "buybox": 29.99,
        "amazon_buybox_pct": 12.7,
    }


def test_check_eligibility_fully_eligible():
    result = check_eligibility(_base_eligible_fixture())
    assert result["eligible"] is True
    assert result["filter_failed"] is None
    for name, check in result["checks"].items():
        assert check["pass"] is True, f"{name} unexpectedly failed"


def test_check_eligibility_fails_rule1_referral_fee():
    # referral_fee_pct missing (None) -- everything else fine.
    data = _base_eligible_fixture()
    data["referral_fee_pct"] = None
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "referral_fee_pct"
    assert result["checks"]["referral_fee_pct"]["pass"] is False
    # confirm no earlier rule could have failed (there is none before rule 1)


def test_check_eligibility_fails_rule1_referral_fee_zero():
    # referral_fee_pct present but not > 0.
    data = _base_eligible_fixture()
    data["referral_fee_pct"] = 0
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "referral_fee_pct"


def test_check_eligibility_fails_rule2_rank_and_no_demand_exemption():
    # rank > 100,000 AND monthly_sold not >= 100 (None) -- fails rank only.
    data = _base_eligible_fixture()
    data["sales_rank"] = 164_080
    data["monthly_sold"] = None
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "rank"
    # rule 1 must have passed for this to be unambiguous
    assert result["checks"]["referral_fee_pct"]["pass"] is True


def test_check_eligibility_fails_rule3_buybox_too_low():
    data = _base_eligible_fixture()
    data["buybox"] = 5.00  # < $10 threshold
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "buybox"
    assert result["checks"]["referral_fee_pct"]["pass"] is True
    assert result["checks"]["rank"]["pass"] is True


def test_check_eligibility_fails_rule4_amazon_dominance():
    data = _base_eligible_fixture()
    data["amazon_buybox_pct"] = 95.0  # > 80 threshold
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "amazon_pct"
    assert result["checks"]["referral_fee_pct"]["pass"] is True
    assert result["checks"]["rank"]["pass"] is True
    assert result["checks"]["buybox"]["pass"] is True


def test_check_eligibility_fails_rule5_monthly_sold_below_threshold():
    # monthly_sold present but < 100 -- fails rule 5.
    # Must still pass rule 2 (rank alone is fine, <=100000), so this
    # isolates rule 5 specifically.
    data = _base_eligible_fixture()
    data["sales_rank"] = 50_000  # passes rule 2 on rank alone
    data["monthly_sold"] = 42
    result = check_eligibility(data)
    assert result["eligible"] is False
    assert result["filter_failed"] == "monthly_sold"
    assert result["checks"]["rank"]["pass"] is True  # rank alone satisfies rule 2


def test_check_eligibility_demand_exemption_overrides_bad_rank():
    # rank > 100,000 but monthly_sold >= 100 -- rule 2 should still PASS
    # via the demand exemption, and (since monthly_sold >= 100) rule 5
    # also passes.
    data = _base_eligible_fixture()
    data["sales_rank"] = 500_000
    data["monthly_sold"] = 150
    result = check_eligibility(data)
    assert result["checks"]["rank"]["pass"] is True
    assert result["checks"]["monthly_sold"]["pass"] is True
    assert result["eligible"] is True
    assert result["filter_failed"] is None
