"""Router-level tests for GET /eligibility/{asin} and POST /eligibility/batch.

The `client` fixture (conftest.py) hands each HTTP request its own fresh
session bound to `test_engine` (see `_override_get_db`), not a per-test
rolled-back transaction the way `db_session` is -- so fixture rows here are
inserted directly against `test_engine` and cleaned up in a `finally`
block, same pattern as tests/test_etl_dirty_data.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asin import Asin, AsinPriceStats

pytestmark = pytest.mark.asyncio


async def _register(client) -> str:
    resp = await client.post(
        "/auth/register",
        json={"email": f"elig-{uuid.uuid4().hex}@example.com", "password": "correct horse"},
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


async def _insert_asin(test_engine, **overrides) -> str:
    asin = overrides.pop("asin", f"TESTELIG{uuid.uuid4().hex[:8].upper()}")
    defaults = dict(
        asin=asin,
        title="Test Widget",
        buybox=29.99,
        referral_fee_pct=15,
        sales_rank=88_003,
        amazon_buybox_pct=12.7,
        monthly_sold=None,
        eligible=True,
        filter_failed=None,
        computed_roi_pct=131.1,
        supplier_cost=9.27,
        snapshot_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)

    session_maker = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        session.add(Asin(**defaults))
        await session.commit()
    return asin


async def _insert_price_stats(test_engine, asin: str, **overrides) -> None:
    defaults = dict(
        asin=asin,
        avg_90d=29.99,
        min_90d=24.99,
        current_deviation_pct=0.0,
        computed_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    session_maker = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        session.add(AsinPriceStats(**defaults))
        await session.commit()


async def _cleanup(test_engine, asin: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM asin_price_stats WHERE asin = :asin"), {"asin": asin}
        )
        await conn.execute(text("DELETE FROM asins WHERE asin = :asin"), {"asin": asin})


async def test_get_eligibility_requires_auth(client):
    resp = await client.get("/eligibility/B00HEON30Y")
    assert resp.status_code == 401


async def test_get_eligibility_unknown_asin_404(client):
    token = await _register(client)
    resp = await client.get(
        "/eligibility/DOES_NOT_EXIST", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


async def test_get_eligibility_known_asin_matches_challenge_shape(client, test_engine):
    asin = await _insert_asin(test_engine)
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["asin"] == asin
        assert body["eligible"] is True
        assert body["filter_failed"] is None
        assert body["checks"]["buybox"]["pass"] is True
        assert body["computed_roi_pct"] == pytest.approx(131.1)
        assert body["supplier_cost"] == pytest.approx(9.27)
        assert body["buybox"] == pytest.approx(29.99)
        assert body["amazon_buybox_pct"] == pytest.approx(12.7)
        assert body["snapshot_at"] is not None
        assert "data_freshness_note" not in body
        assert "price_anomaly_note" not in body
    finally:
        await _cleanup(test_engine, asin)


async def test_get_eligibility_first_failing_rule_recorded(client, test_engine):
    # sales_rank over threshold, no monthly_sold override -> fails "rank".
    asin = await _insert_asin(
        test_engine, sales_rank=164_080, monthly_sold=None, eligible=False, filter_failed="rank"
    )
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["eligible"] is False
        assert body["filter_failed"] == "rank"
        assert body["checks"]["rank"]["pass"] is False
    finally:
        await _cleanup(test_engine, asin)


async def test_get_eligibility_stale_snapshot_gets_freshness_note(client, test_engine):
    stale_time = datetime.now(timezone.utc) - timedelta(hours=26)
    asin = await _insert_asin(test_engine, snapshot_at=stale_time)
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data_freshness_note" in body
        assert "26h" in body["data_freshness_note"]
    finally:
        await _cleanup(test_engine, asin)


async def test_get_eligibility_fresh_snapshot_has_no_freshness_note(client, test_engine):
    asin = await _insert_asin(test_engine, snapshot_at=datetime.now(timezone.utc))
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "data_freshness_note" not in resp.json()
    finally:
        await _cleanup(test_engine, asin)


async def test_get_eligibility_price_anomaly_note(client, test_engine):
    # buybox = avg_90d * 3 -> comfortably past the 30% anomaly threshold.
    asin = await _insert_asin(test_engine, buybox=90.0)
    await _insert_price_stats(test_engine, asin, avg_90d=30.0)
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "price_anomaly_note" in body
        note = body["price_anomaly_note"].lower()
        assert "anomaly" in note or "deviat" in note
    finally:
        await _cleanup(test_engine, asin)


async def test_get_eligibility_no_anomaly_when_within_threshold(client, test_engine):
    asin = await _insert_asin(test_engine, buybox=31.0)
    await _insert_price_stats(test_engine, asin, avg_90d=30.0)
    try:
        token = await _register(client)
        resp = await client.get(
            f"/eligibility/{asin}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "price_anomaly_note" not in resp.json()
    finally:
        await _cleanup(test_engine, asin)


async def test_batch_eligibility_preserves_order_and_handles_unknown(client, test_engine):
    asin_a = await _insert_asin(test_engine)
    asin_b = await _insert_asin(test_engine)
    try:
        token = await _register(client)
        resp = await client.post(
            "/eligibility/batch",
            json={"asins": [asin_a, "DOES_NOT_EXIST", asin_b]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        assert body[0]["asin"] == asin_a
        assert body[1] == {"asin": "DOES_NOT_EXIST", "error": "not_found"}
        assert body[2]["asin"] == asin_b
    finally:
        await _cleanup(test_engine, asin_a)
        await _cleanup(test_engine, asin_b)


async def test_batch_eligibility_requires_auth(client):
    resp = await client.post("/eligibility/batch", json={"asins": ["X"]})
    assert resp.status_code == 401


async def test_batch_eligibility_empty_list(client):
    token = await _register(client)
    resp = await client.post(
        "/eligibility/batch", json={"asins": []}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []
