"""Eligibility rules + ROI/payout formulas — pure arithmetic, no LLM, no I/O.

`compute_payout`/`compute_roi` are copied verbatim from CHALLENGE.md's
"ROI 公式（照着写，不要自己发明)" section — do not "clean up" the formula,
it's given as-is on purpose.

`check_eligibility` implements the 5-rule table from CHALLENGE.md's
"Eligibility 规则" section, in order, recording the first failing check's
name into `filter_failed` (or None if all 5 pass).
"""
from typing import Any


def compute_payout(buybox, referral_fee_pct, fba_pick_pack_cents):
    referral = buybox * (referral_fee_pct / 100)
    fba = fba_pick_pack_cents / 100   # Keepa 返回的是 cents
    storage = 0.50                     # 月度仓储估算
    return buybox - referral - fba - storage


def compute_roi(buybox, referral_pct, fba_pick_pack_cents, supplier_cost, n_items):
    payout = compute_payout(buybox, referral_pct, fba_pick_pack_cents)
    cost = supplier_cost * max(n_items or 1, 1)
    return None if cost <= 0 else 100 * (payout - cost) / cost


# --- eligibility -------------------------------------------------------

RANK_THRESHOLD = 100_000
BUYBOX_THRESHOLD = 10
AMAZON_PCT_THRESHOLD = 80
MONTHLY_SOLD_THRESHOLD = 100


def check_eligibility(asin_data: dict[str, Any]) -> dict[str, Any]:
    """Run the 5 eligibility rules against a dict of ASIN fields.

    Expects `asin_data` to contain (missing keys treated as None):
        referral_fee_pct, sales_rank, monthly_sold, buybox, amazon_buybox_pct

    Returns:
        {
          "eligible": bool,
          "filter_failed": str | None,   # name of the FIRST failing check
          "checks": {
              "referral_fee_pct": {"pass": bool, "value": ...},
              "rank":             {"pass": bool, "value": ..., "threshold": 100000},
              "buybox":           {"pass": bool, "value": ..., "threshold": 10},
              "amazon_pct":       {"pass": bool, "value": ..., "threshold": 80},
              "monthly_sold":     {"pass": bool, "value": ...},
          }
        }

    Shape mirrors CHALLENGE.md's `/eligibility` example as closely as
    reasonable so later phases (the actual /eligibility endpoint) can
    consume this directly.
    """
    referral_fee_pct = asin_data.get("referral_fee_pct")
    sales_rank = asin_data.get("sales_rank")
    monthly_sold = asin_data.get("monthly_sold")
    buybox = asin_data.get("buybox")
    amazon_buybox_pct = asin_data.get("amazon_buybox_pct")

    # Rule 1: referral_fee_pct present and > 0.
    check_referral = {
        "pass": referral_fee_pct is not None and referral_fee_pct > 0,
        "value": referral_fee_pct,
    }

    # Rule 2: sales_rank <= 100,000 OR monthly_sold >= 100 (demand exemption).
    rank_ok = sales_rank is not None and sales_rank <= RANK_THRESHOLD
    demand_exempt = monthly_sold is not None and monthly_sold >= MONTHLY_SOLD_THRESHOLD
    check_rank = {
        "pass": rank_ok or demand_exempt,
        "value": sales_rank,
        "threshold": RANK_THRESHOLD,
    }

    # Rule 3: buybox >= $10.
    check_buybox = {
        "pass": buybox is not None and buybox >= BUYBOX_THRESHOLD,
        "value": buybox,
        "threshold": BUYBOX_THRESHOLD,
    }

    # Rule 4: amazon_buybox_pct <= 80.
    check_amazon_pct = {
        "pass": amazon_buybox_pct is None or amazon_buybox_pct <= AMAZON_PCT_THRESHOLD,
        "value": amazon_buybox_pct,
        "threshold": AMAZON_PCT_THRESHOLD,
    }
    # Note: amazon_buybox_pct missing (None) is treated as passing this rule
    # (no evidence of Amazon dominance) — mirrors the "don't fabricate
    # failure from missing data" dirty-data stance used for monthly_sold.

    # Rule 5: monthly_sold is null OR >= 100.
    check_monthly_sold = {
        "pass": monthly_sold is None or monthly_sold >= MONTHLY_SOLD_THRESHOLD,
        "value": monthly_sold,
    }

    checks = {
        "referral_fee_pct": check_referral,
        "rank": check_rank,
        "buybox": check_buybox,
        "amazon_pct": check_amazon_pct,
        "monthly_sold": check_monthly_sold,
    }

    filter_failed = None
    for name, check in checks.items():
        if not check["pass"]:
            filter_failed = name
            break

    return {
        "eligible": filter_failed is None,
        "filter_failed": filter_failed,
        "checks": checks,
    }
