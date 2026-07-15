"""Shared ETL/refresh logic: fetch ASIN(s) from Keepa, parse, compute
eligibility/ROI/90-day stats, and idempotently upsert into `asins` +
`asin_price_stats`.

This module is deliberately NOT `app/etl.py` -- Phase 3b's Celery refresh
task imports these functions directly so the "fetch -> parse -> compute ->
upsert" body exists in exactly one place, shared by:
  - `app/etl.py`'s `python -m app.etl` one-shot batch load
  - Phase 3b's `POST /refresh` background job (per-ASIN Celery tasks)
  - a possible future "ASIN not in DB yet -- fetch it live" on-demand path
    (CHALLENGE.md's 加分项 list)

Per-item failure isolation is the core contract here: neither
`fetch_and_upsert_asin` nor `fetch_and_upsert_batch` ever raises for a
single bad/missing/error-prone ASIN -- both catch and report per item,
because both ETL (a batch of 32) and refresh (background job over the
whole catalog) need one bad ASIN to not take down the rest of the run.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.eligibility import check_eligibility, compute_roi
from app.keepa.client import KeepaClient
from app.keepa.parse import (
    extract_amazon_buybox_pct,
    extract_avg90_buybox,
    extract_current_buybox,
    extract_fba_pick_pack_cents,
    extract_min90_buybox,
    extract_monthly_sold,
    extract_number_of_items,
    extract_referral_fee_pct,
    extract_sales_rank,
)
from app.models.asin import Asin, AsinPriceStats

# --- price anomaly -----------------------------------------------------

# Threshold reasoning (CHALLENGE.md: "阈值你定并说明理由"):
# Keepa BuyBox prices routinely swing +/-10-20% around their 90-day average
# from ordinary repricing/competition -- especially on mixed FBA/FBM
# listings -- without that meaning anything is wrong or that there's a new
# arbitrage opportunity. A tight threshold (say 10-15%) would flag that
# everyday noise on a large slice of the catalog, making the signal useless
# ("everything is an anomaly" = nothing is). 30% is chosen as comfortably
# outside ordinary day-to-day repricing noise while still being tight enough
# to catch the cases that actually matter to a reseller deciding whether to
# buy right now: a stockout price spike, a competitor's clearance crash, or
# a stale/bad snapshot. The check is symmetric (over OR under 90d avg by
# >30%) since both directions are actionable -- a crash means "great deal,
# verify it's real before buying"; a spike means "don't buy at today's price,
# it'll likely revert."
PRICE_ANOMALY_THRESHOLD_PCT = 30


def is_price_anomaly(
    current: float | None,
    avg_90d: float | None,
    threshold_pct: float = PRICE_ANOMALY_THRESHOLD_PCT,
) -> bool:
    """True if `current` deviates from `avg_90d` by more than `threshold_pct` percent.

    None-safe: missing current price or missing/zero 90-day average can't
    support a deviation claim, so this returns False (not an anomaly) rather
    than raising or fabricating a signal from absent data.
    """
    if current is None or avg_90d is None or avg_90d == 0:
        return False
    deviation_pct = abs(current - avg_90d) / avg_90d * 100
    return deviation_pct > threshold_pct


# --- upsert helpers ------------------------------------------------------


async def _upsert_asin_row(session: AsyncSession, values: dict[str, Any]) -> None:
    stmt = pg_insert(Asin).values(**values)
    update_cols = {col: stmt.excluded[col] for col in values if col != "asin"}
    stmt = stmt.on_conflict_do_update(index_elements=["asin"], set_=update_cols)
    await session.execute(stmt)


async def _upsert_price_stats_row(session: AsyncSession, values: dict[str, Any]) -> None:
    stmt = pg_insert(AsinPriceStats).values(**values)
    update_cols = {col: stmt.excluded[col] for col in values if col != "asin"}
    stmt = stmt.on_conflict_do_update(index_elements=["asin"], set_=update_cols)
    await session.execute(stmt)


async def _process_product(
    session: AsyncSession,
    asin: str,
    product: dict[str, Any],
    supplier_cost: float | None,
) -> dict[str, Any]:
    """Parse one raw Keepa `product` dict, compute eligibility/ROI/90-day
    stats, and upsert `asins` + `asin_price_stats` for `asin`.

    Private per-product helper shared by `fetch_and_upsert_asin` (one
    product) and `fetch_and_upsert_batch` (N products from a single batched
    Keepa call) -- this is the ONE place the parse/eligibility/upsert body
    lives, so neither caller duplicates it.

    Never raises: any exception while parsing/computing/writing this single
    product is caught and reported as `status="error"`, isolated via a
    SAVEPOINT (`session.begin_nested()`) so a DB-level failure on this one
    row (e.g. a constraint violation) doesn't poison the whole session's
    transaction for the other items a caller processes with it.
    """
    try:
        title = product.get("title")
        buybox = extract_current_buybox(product)
        referral_fee_pct = extract_referral_fee_pct(product)
        sales_rank = extract_sales_rank(product)
        amazon_buybox_pct = extract_amazon_buybox_pct(product)
        monthly_sold = extract_monthly_sold(product)
        fba_pick_pack_cents = extract_fba_pick_pack_cents(product)
        n_items = extract_number_of_items(product)
        avg_90d = extract_avg90_buybox(product)
        min_90d = extract_min90_buybox(product)

        elig = check_eligibility(
            {
                "referral_fee_pct": referral_fee_pct,
                "sales_rank": sales_rank,
                "monthly_sold": monthly_sold,
                "buybox": buybox,
                "amazon_buybox_pct": amazon_buybox_pct,
            }
        )

        # compute_roi/compute_payout assume all their numeric inputs are
        # real numbers (they do arithmetic directly, no None-guards inside
        # -- CHALLENGE.md's formula is copied verbatim, not defensively
        # rewritten). Only call it when every required input is present;
        # otherwise ROI is genuinely unknown (None), not fabricated as 0.
        computed_roi_pct = None
        if (
            buybox is not None
            and referral_fee_pct is not None
            and fba_pick_pack_cents is not None
            and supplier_cost is not None
        ):
            computed_roi_pct = compute_roi(
                buybox, referral_fee_pct, fba_pick_pack_cents, supplier_cost, n_items
            )

        current_deviation_pct = None
        if buybox is not None and avg_90d:
            current_deviation_pct = 100 * (buybox - avg_90d) / avg_90d

        now = datetime.now(timezone.utc)

        async with session.begin_nested():
            await _upsert_asin_row(
                session,
                {
                    "asin": asin,
                    "title": title,
                    "buybox": buybox,
                    "referral_fee_pct": referral_fee_pct,
                    "sales_rank": sales_rank,
                    "amazon_buybox_pct": amazon_buybox_pct,
                    "monthly_sold": monthly_sold,
                    "eligible": elig["eligible"],
                    "filter_failed": elig["filter_failed"],
                    "computed_roi_pct": computed_roi_pct,
                    "supplier_cost": supplier_cost,
                    "snapshot_at": now,
                },
            )
            await _upsert_price_stats_row(
                session,
                {
                    "asin": asin,
                    "avg_90d": avg_90d,
                    "min_90d": min_90d,
                    "current_deviation_pct": current_deviation_pct,
                    "computed_at": now,
                },
            )

        return {"asin": asin, "status": "ok", "error": None}
    except Exception as exc:  # noqa: BLE001 -- per-item isolation is the point
        return {"asin": asin, "status": "error", "error": str(exc)}


def _index_products_by_asin(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{asin: product}` from a Keepa `/product` response, skipping `null`
    entries (Keepa pads `products[]` with `null` for ASINs it doesn't
    recognize, rather than omitting them, so the array stays aligned with
    the request -- defend against both that and simple omission)."""
    products = response.get("products") or []
    return {p["asin"]: p for p in products if p and p.get("asin")}


# --- public entrypoints --------------------------------------------------


async def fetch_and_upsert_asin(
    session: AsyncSession,
    keepa_client: KeepaClient,
    asin: str,
    supplier_cost: float | None,
) -> dict[str, Any]:
    """Fetch ONE ASIN from Keepa and upsert it. Never raises.

    Returns `{"asin", "status": "ok"|"not_found"|"error", "error"}`.
    """
    try:
        response = await keepa_client.get_products(asins=[asin])
    except Exception as exc:  # noqa: BLE001
        return {"asin": asin, "status": "error", "error": str(exc)}

    by_asin = _index_products_by_asin(response)
    product = by_asin.get(asin)
    if product is None:
        # "Not found" = Keepa's response didn't include this ASIN. We do NOT
        # touch any pre-existing DB row for it (no delete, no null-out) --
        # a single miss on one call isn't strong enough evidence the ASIN no
        # longer exists, and `asins` is meant to hold "last known good"
        # data. See fetch_and_upsert_batch's docstring for the same call.
        return {"asin": asin, "status": "not_found", "error": None}

    result = await _process_product(session, asin, product, supplier_cost)
    await session.commit()
    return result


async def fetch_and_upsert_batch(
    session: AsyncSession,
    keepa_client: KeepaClient,
    asin_supplier_costs: dict[str, float | None],
) -> list[dict[str, Any]]:
    """Fetch MANY ASINs via a single (client-internally-chunked) Keepa call
    and upsert each. Never raises.

    `asin_supplier_costs` maps asin -> supplier_cost (may be None).  Results
    are returned in the same order as `asin_supplier_costs`'s keys.

    "Not found" semantics: an ASIN present in the request but absent from
    Keepa's response gets `status="not_found"` and its row (if any already
    exists in the DB from a prior run) is left completely alone -- we do
    not upsert a blank/null row over it and we do not delete it. Rationale:
    a single batched call returning nothing for one ASIN is weak evidence
    (could be a transient Keepa-side gap), so the DB keeps whatever the last
    successful snapshot was rather than being clobbered by silence.

    If the Keepa call itself fails (rate limit exhausted, all keys out of
    tokens, network error, ...) that's a whole-batch failure, not a
    per-ASIN one -- every requested ASIN is reported `status="error"` with
    the same underlying error message, and (like every other path here)
    nothing is raised to the caller.
    """
    asins = list(asin_supplier_costs.keys())
    if not asins:
        return []

    try:
        response = await keepa_client.get_products(asins=asins)
    except Exception as exc:  # noqa: BLE001
        return [{"asin": a, "status": "error", "error": str(exc)} for a in asins]

    by_asin = _index_products_by_asin(response)

    results: list[dict[str, Any]] = []
    for asin in asins:
        product = by_asin.get(asin)
        if product is None:
            results.append({"asin": asin, "status": "not_found", "error": None})
            continue
        result = await _process_product(session, asin, product, asin_supplier_costs[asin])
        results.append(result)

    await session.commit()
    return results
