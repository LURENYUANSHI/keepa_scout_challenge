"""Feeds app.ingest a deliberately dirty/incomplete Keepa product payload
(-1 sentinels, missing `fbaFees`/`monthlySold` keys entirely) and asserts:
  - fetch_and_upsert_asin doesn't raise
  - the resulting `asins`/`asin_price_stats` rows have None (not 0/crash)
    in every field that had no real data
  - running it twice against the same ASIN doesn't create a duplicate row
    (upsert, not insert) -- HARNESS.md §4's idempotency requirement.

Uses respx to mock Keepa's HTTP response -- no real network call. This
test deliberately does NOT use conftest.py's `db_session` fixture: that
fixture wraps each test in a transaction that gets rolled back at
teardown, but these tests need `fetch_and_upsert_asin`'s internal
`session.commit()` calls to actually land and be independently
re-queryable across two separate calls (to prove idempotency), so each
test opens its own session(s) against the shared `test_engine` fixture and
cleans up its own rows in a `finally` block instead.
"""
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest import fetch_and_upsert_asin
from app.keepa.client import KeepaClient
from app.models.asin import Asin, AsinPriceStats

pytestmark = pytest.mark.asyncio


def _dirty_product(asin: str, buybox_cents: int = -1) -> dict:
    """A Keepa product payload missing/`-1`-sentinel'd on purpose:
      - stats.current is all -1 (no BuyBox, no sales rank -- indices 18/3)
      - stats.avg90 is all -1 (no 90-day average)
      - no `stats.min` key at all (missing, not just -1)
      - no `fbaFees` key at all (not even an empty dict)
      - no `monthlySold` key at all
      - referralFeePercentage is -1 (no data)
    """
    current = [-1] * 19
    current[18] = buybox_cents
    avg90 = [-1] * 19
    return {
        "asin": asin,
        "title": "Dirty Test Product",
        "referralFeePercentage": -1,
        "stats": {
            "current": current,
            "avg90": avg90,
        },
    }


async def _cleanup(test_engine, asin: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM asin_price_stats WHERE asin = :asin"), {"asin": asin}
        )
        await conn.execute(text("DELETE FROM asins WHERE asin = :asin"), {"asin": asin})


@respx.mock
async def test_dirty_product_does_not_raise_and_stores_none_not_zero(test_engine):
    asin = f"TESTDIRTY{uuid.uuid4().hex[:8].upper()}"

    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(
            200, json={"tokensLeft": 100, "products": [_dirty_product(asin)]}
        )
    )

    session_maker = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    keepa_client = KeepaClient(api_keys=["dummy-key"])

    try:
        async with session_maker() as session:
            result = await fetch_and_upsert_asin(
                session, keepa_client, asin, supplier_cost=10.0
            )

        assert result == {"asin": asin, "status": "ok", "error": None}

        async with session_maker() as session:
            row = await session.get(Asin, asin)
            assert row is not None
            # Dirty/missing fields must come through as None, never 0.
            assert row.buybox is None
            assert row.sales_rank is None
            assert row.referral_fee_pct is None
            assert row.monthly_sold is None
            assert row.amazon_buybox_pct is None
            # ROI can't be computed without a real buybox/referral/fba fee --
            # None (unknown), not a fabricated 0 or crash.
            assert row.computed_roi_pct is None
            # Not eligible: the first failing rule is referral_fee_pct
            # (present-and->0 required; here it's missing/-1 -> None).
            assert row.eligible is False
            assert row.filter_failed == "referral_fee_pct"

            stats_row = await session.get(AsinPriceStats, asin)
            assert stats_row is not None
            assert stats_row.avg_90d is None
            assert stats_row.min_90d is None  # "min" key was absent entirely
            assert stats_row.current_deviation_pct is None
    finally:
        await _cleanup(test_engine, asin)


@respx.mock
async def test_running_twice_upserts_not_duplicates(test_engine):
    asin = f"TESTIDEMP{uuid.uuid4().hex[:8].upper()}"

    route = respx.get("https://api.keepa.com/product")
    call_count = {"n": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # Second call returns different data, so a passing "1 row, updated
        # value" assertion actually proves UPDATE happened -- not just that
        # the second call was a silent no-op.
        buybox_cents = 2999 if call_count["n"] == 2 else -1
        product = _dirty_product(asin, buybox_cents=buybox_cents)
        return httpx.Response(200, json={"tokensLeft": 100, "products": [product]})

    route.mock(side_effect=_responder)

    session_maker = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    keepa_client = KeepaClient(api_keys=["dummy-key"])

    try:
        async with session_maker() as session:
            first = await fetch_and_upsert_asin(session, keepa_client, asin, supplier_cost=10.0)
        async with session_maker() as session:
            second = await fetch_and_upsert_asin(session, keepa_client, asin, supplier_cost=10.0)

        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert call_count["n"] == 2

        async with session_maker() as session:
            result = await session.execute(select(Asin).where(Asin.asin == asin))
            rows = result.scalars().all()
            assert len(rows) == 1  # upsert, not insert -- no duplicate row
            assert float(rows[0].buybox) == 29.99  # picked up the 2nd call's value

            stats_result = await session.execute(
                select(AsinPriceStats).where(AsinPriceStats.asin == asin)
            )
            stats_rows = stats_result.scalars().all()
            assert len(stats_rows) == 1
    finally:
        await _cleanup(test_engine, asin)
