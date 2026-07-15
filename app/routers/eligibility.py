"""GET /eligibility/{asin}, POST /eligibility/batch.

Both read ONLY from `asins`/`asin_price_stats` -- neither calls Keepa live.
Per ARCHITECTURE.md §1: "`/upc`、`/eligibility` 这种单次查询由 `api` 直接同步调
Keepa" is about `/upc`; `/eligibility` here is explicitly DB-only (a live
refetch is ETL's or `/refresh`'s job, not a read endpoint's). If the ASIN
isn't in the DB yet, that's a 404 (single) / a `{"asin","error":"not_found"}`
marker (batch) -- not an on-demand Keepa call. (CHALLENGE.md lists "被问到
库里没有的 ASIN 时，现场调 Keepa 补拉" as an optional 加分项, not required;
left undone here, see this file's usage in REPORT.md's "故意没做好" list.)
"""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db import get_db
from app.eligibility import check_eligibility
from app.ingest import is_price_anomaly
from app.models.asin import Asin, AsinPriceStats
from app.models.user import User
from app.schemas.eligibility import BatchEligibilityRequest

router = APIRouter(tags=["eligibility"])

# HARNESS.md §5: snapshots older than this get a staleness note woven into
# the response.
STALE_AFTER_HOURS = 24

# Defensive chunking for POST /eligibility/batch's `WHERE asin IN (...)`
# clause -- HARNESS.md §3 wants "150+ ASIN 自动分块" so a very long input
# list doesn't become one pathological SQL statement. This is DB reads, not
# live Keepa calls, so there's no hard batch-size ceiling from Keepa's side;
# this number is just "comfortably below anything that'd bloat a single
# query plan," not a documented Postgres limit.
_IN_CLAUSE_CHUNK_SIZE = 500


def _to_float(value: Decimal | float | None) -> float | None:
    """Postgres NUMERIC columns come back as `Decimal` via SQLAlchemy --
    normalize to `float` for JSON responses and for arithmetic against the
    plain floats `is_price_anomaly`/`check_eligibility` expect."""
    return float(value) if value is not None else None


def _serialize(asin_row: Asin, stats_row: AsinPriceStats | None) -> dict:
    """Build one `/eligibility` response body (shape per CHALLENGE.md's
    `/eligibility` example) from a DB row pair.

    `eligible`/`filter_failed`/`checks` are recomputed here via
    `check_eligibility()` from the stored raw fields, rather than trusting
    `asins.eligible`/`filter_failed` verbatim. Those two columns are the
    precomputed-at-ETL-time source of truth used for indexed SQL filtering
    (CHALLENGE.md's "预计算的 eligibility 布尔值" requirement, and
    app/models/asin.py's indexes) -- but recomputing for *this* response
    body is free (pure arithmetic, no I/O) and keeps the displayed `checks`
    breakdown self-consistent even when a raw field is updated out from
    under the precomputed columns, exactly what HARNESS.md §5's anomaly
    test does (`UPDATE asins SET buybox = avg_90d * 3 ...` via psql,
    without re-running ETL).
    """
    elig = check_eligibility(
        {
            "referral_fee_pct": _to_float(asin_row.referral_fee_pct),
            "sales_rank": asin_row.sales_rank,
            "monthly_sold": _to_float(asin_row.monthly_sold),
            "buybox": _to_float(asin_row.buybox),
            "amazon_buybox_pct": _to_float(asin_row.amazon_buybox_pct),
        }
    )

    body: dict = {
        "asin": asin_row.asin,
        "title": asin_row.title,
        "eligible": elig["eligible"],
        "filter_failed": elig["filter_failed"],
        "checks": elig["checks"],
        "computed_roi_pct": _to_float(asin_row.computed_roi_pct),
        "supplier_cost": _to_float(asin_row.supplier_cost),
        "buybox": _to_float(asin_row.buybox),
        "amazon_buybox_pct": _to_float(asin_row.amazon_buybox_pct),
        "snapshot_at": asin_row.snapshot_at,
    }

    # --- staleness note (HARNESS.md §5 / CHALLENGE.md "数据新鲜度") --------
    if asin_row.snapshot_at is not None:
        now = datetime.now(timezone.utc)
        age_hours = (now - asin_row.snapshot_at).total_seconds() / 3600
        if age_hours > STALE_AFTER_HOURS:
            body["data_freshness_note"] = (
                f"data last refreshed {int(age_hours)}h ago — consider POST /refresh"
            )

    # --- price anomaly note (HARNESS.md §5 / CHALLENGE.md "价格异常") ------
    current_buybox = _to_float(asin_row.buybox)
    avg_90d = _to_float(stats_row.avg_90d) if stats_row else None
    if is_price_anomaly(current_buybox, avg_90d):
        deviation_pct = round(100 * (current_buybox - avg_90d) / avg_90d, 1)
        body["price_anomaly_note"] = (
            f"buybox (${current_buybox}) deviates {deviation_pct}% from the "
            f"90-day average (${avg_90d}) — possible price anomaly"
        )

    return body


@router.get("/eligibility/{asin}")
async def get_eligibility(
    asin: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    asin_row = await db.get(Asin, asin)
    if asin_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ASIN {asin!r} not found — has it been ETL'd / refreshed yet?",
        )
    stats_row = await db.get(AsinPriceStats, asin)
    return _serialize(asin_row, stats_row)


@router.post("/eligibility/batch")
async def batch_eligibility(
    body: BatchEligibilityRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    asins = body.asins
    if not asins:
        return []

    found_asins: dict[str, Asin] = {}
    for i in range(0, len(asins), _IN_CLAUSE_CHUNK_SIZE):
        chunk = asins[i : i + _IN_CLAUSE_CHUNK_SIZE]
        result = await db.execute(select(Asin).where(Asin.asin.in_(chunk)))
        for row in result.scalars():
            found_asins[row.asin] = row

    stats_by_asin: dict[str, AsinPriceStats] = {}
    if found_asins:
        keys = list(found_asins.keys())
        for i in range(0, len(keys), _IN_CLAUSE_CHUNK_SIZE):
            chunk = keys[i : i + _IN_CLAUSE_CHUNK_SIZE]
            result = await db.execute(
                select(AsinPriceStats).where(AsinPriceStats.asin.in_(chunk))
            )
            for row in result.scalars():
                stats_by_asin[row.asin] = row

    results: list[dict] = []
    for asin in asins:
        row = found_asins.get(asin)
        if row is None:
            # Graceful "not found" marker, matching HARNESS.md §3's "混入
            # Keepa 查不到的 ASIN 不 500，优雅返回该项的 null/错误标记" --
            # never let one bad ASIN 500 the whole batch response.
            results.append({"asin": asin, "error": "not_found"})
            continue
        results.append(_serialize(row, stats_by_asin.get(asin)))

    return results
