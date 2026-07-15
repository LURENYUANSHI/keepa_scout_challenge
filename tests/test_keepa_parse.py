"""Unit tests for app/keepa/parse.py — pure functions, no I/O, no network.

Covers: keepa_time_to_datetime, safe_value/cents_to_dollars (-1/None
sentinel handling), and extract_amazon_buybox_pct against a small
synthetic buyBoxSellerIdHistory fixture with a hand-computed expected %.
"""
from datetime import datetime, timezone

from app.keepa.parse import (
    AMAZON_SELLER_ID,
    cents_to_dollars,
    extract_amazon_buybox_pct,
    extract_current_buybox,
    extract_fba_pick_pack_cents,
    extract_monthly_sold,
    extract_referral_fee_pct,
    extract_sales_rank,
    keepa_time_to_datetime,
    safe_value,
)


# --- keepa_time_to_datetime ------------------------------------------------


def test_keepa_time_to_datetime_epoch_zero():
    # keepaMinute 0 is, by construction of Keepa's time format, the Keepa
    # "epoch" -- 2011-01-01 00:00:00 UTC. This is a well-known fixed point,
    # independent of re-deriving the formula in the test.
    result = keepa_time_to_datetime(0)
    assert result == datetime(2011, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_keepa_time_to_datetime_known_value():
    # 100_000 keepa-minutes after the epoch -> 2011-03-11 10:40:00 UTC
    # (independently verified: (100000 + 21564000) * 60 = 1299840000 unix
    # seconds, which is 2011-03-11T10:40:00+00:00).
    result = keepa_time_to_datetime(100_000)
    assert result == datetime(2011, 3, 11, 10, 40, 0, tzinfo=timezone.utc)


def test_keepa_time_to_datetime_is_utc_aware():
    result = keepa_time_to_datetime(6_908_240)
    assert result.utcoffset().total_seconds() == 0


# --- safe_value / cents_to_dollars -----------------------------------------


def test_safe_value_minus_one_is_none():
    assert safe_value(-1) is None


def test_safe_value_none_is_none():
    assert safe_value(None) is None


def test_safe_value_real_value_passthrough():
    assert safe_value(0) == 0
    assert safe_value(42) == 42
    assert safe_value(3.14) == 3.14


def test_cents_to_dollars_minus_one_is_none():
    assert cents_to_dollars(-1) is None


def test_cents_to_dollars_none_is_none():
    assert cents_to_dollars(None) is None


def test_cents_to_dollars_real_value():
    assert cents_to_dollars(2999) == 29.99
    assert cents_to_dollars(0) == 0
    assert cents_to_dollars(100_000) == 1000.0


# --- extract_current_buybox / extract_sales_rank ---------------------------


def test_extract_current_buybox_from_stats_current():
    product = {"stats": {"current": [0] * 19}}
    product["stats"]["current"][18] = 2999  # BUY_BOX_SHIPPING index
    assert extract_current_buybox(product) == 29.99


def test_extract_current_buybox_minus_one_is_none():
    product = {"stats": {"current": [0] * 19}}
    product["stats"]["current"][18] = -1
    assert extract_current_buybox(product) is None


def test_extract_current_buybox_missing_stats_is_none():
    assert extract_current_buybox({}) is None
    assert extract_current_buybox({"stats": {}}) is None


def test_extract_sales_rank_from_stats_current():
    product = {"stats": {"current": [0] * 4}}
    product["stats"]["current"][3] = 88_003  # SALES index
    assert extract_sales_rank(product) == 88_003


def test_extract_sales_rank_minus_one_is_none():
    product = {"stats": {"current": [0] * 4}}
    product["stats"]["current"][3] = -1
    assert extract_sales_rank(product) is None


# --- extract_referral_fee_pct / extract_fba_pick_pack_cents / monthly_sold -


def test_extract_referral_fee_pct_prefers_new_field():
    product = {"referralFeePercentage": 15, "referralFeePercent": 8}
    assert extract_referral_fee_pct(product) == 15


def test_extract_referral_fee_pct_falls_back_to_deprecated():
    product = {"referralFeePercent": 8}
    assert extract_referral_fee_pct(product) == 8


def test_extract_referral_fee_pct_minus_one_is_none():
    product = {"referralFeePercentage": -1}
    assert extract_referral_fee_pct(product) is None


def test_extract_fba_pick_pack_cents_normal():
    product = {"fbaFees": {"pickAndPackFee": 331}}
    assert extract_fba_pick_pack_cents(product) == 331


def test_extract_fba_pick_pack_cents_missing_fbafees_key():
    assert extract_fba_pick_pack_cents({}) is None


def test_extract_fba_pick_pack_cents_minus_one():
    product = {"fbaFees": {"pickAndPackFee": -1}}
    assert extract_fba_pick_pack_cents(product) is None


def test_extract_monthly_sold_present():
    assert extract_monthly_sold({"monthlySold": 150}) == 150


def test_extract_monthly_sold_missing_is_none():
    assert extract_monthly_sold({}) is None


def test_extract_monthly_sold_minus_one_is_none():
    assert extract_monthly_sold({"monthlySold": -1}) is None


# --- extract_amazon_buybox_pct ----------------------------------------------


def test_extract_amazon_buybox_pct_missing_field_is_none():
    assert extract_amazon_buybox_pct({}) is None
    assert extract_amazon_buybox_pct({"buyBoxSellerIdHistory": []}) is None


def test_extract_amazon_buybox_pct_single_entry_is_none():
    # A single (timestamp, seller) pair has no following boundary, so no
    # segment can be weighted -- not enough data.
    product = {"buyBoxSellerIdHistory": [100000, AMAZON_SELLER_ID]}
    assert extract_amazon_buybox_pct(product) is None


def test_extract_amazon_buybox_pct_closed_window_known_percentage():
    # Construct a history where every segment's boundary is known, and the
    # trailing (last-entry-to-"now") segment is made negligibly small
    # relative to the rest of the window, so the resulting percentage is
    # deterministically close to a hand-computed value:
    #
    #   [-300min, -200min) -> OTHER   (100 min)
    #   [-200min, -100min) -> AMAZON  (100 min)
    #   [-100min, now)     -> OTHER   (~100 min, "now"-open segment)
    #
    # By construction: Amazon holds 100 of ~300 total minutes = ~33.33%.
    now = datetime.now(timezone.utc)
    now_minutes = int(now.timestamp() / 60 - 21564000)

    other = "SOME-OTHER-SELLER-ID"
    history = [
        now_minutes - 300, other,
        now_minutes - 200, AMAZON_SELLER_ID,
        now_minutes - 100, other,
    ]
    product = {"buyBoxSellerIdHistory": history}
    pct = extract_amazon_buybox_pct(product)

    assert pct is not None
    # Tight band around the hand-computed 33.33% -- the only slack is the
    # few milliseconds between this test's `now` and the function's `now`,
    # negligible against a 300-minute window.
    assert 33.0 < pct < 33.7


def test_extract_amazon_buybox_pct_all_amazon():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    now_minutes = int(now.timestamp() / 60 - 21564000)

    history = [
        now_minutes - 1000, AMAZON_SELLER_ID,
        now_minutes - 500, AMAZON_SELLER_ID,
    ]
    product = {"buyBoxSellerIdHistory": history}
    pct = extract_amazon_buybox_pct(product)
    assert pct == 100.0


def test_extract_amazon_buybox_pct_never_amazon():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    now_minutes = int(now.timestamp() / 60 - 21564000)

    history = [
        now_minutes - 1000, "SELLER-A",
        now_minutes - 500, "SELLER-B",
    ]
    product = {"buyBoxSellerIdHistory": history}
    pct = extract_amazon_buybox_pct(product)
    assert pct == 0.0
