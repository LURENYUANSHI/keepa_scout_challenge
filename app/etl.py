"""`python -m app.etl` -- the one-shot batch-load entrypoint (ARCHITECTURE.md
§5, §1's Dockerfile CMD).

Reads `data/sample_asins.csv` (asin, supplier_cost), fetches all of them
from Keepa in one (client-internally-chunked) batched call, and upserts
`asins` + `asin_price_stats` via `app.ingest.fetch_and_upsert_batch` -- this
file has no parsing/eligibility/upsert logic of its own, it's a thin CLI
wrapper so Phase 3b's Celery refresh task can reuse the exact same body
without going through this module at all.

Idempotent / safely re-runnable: `fetch_and_upsert_batch` does an
`INSERT ... ON CONFLICT DO UPDATE` per ASIN (see app/ingest.py), so running
this twice updates the same 32 rows in place -- no duplicates, no error.
"""
import asyncio
import csv
import logging
import math
from pathlib import Path

from app.config import settings
from app.db import async_session_maker, init_db
from app.ingest import fetch_and_upsert_batch
from app.keepa.client import MAX_ASINS_PER_REQUEST, KeepaClient

logger = logging.getLogger("app.etl")

DATA_CSV = Path(__file__).resolve().parent.parent / "data" / "sample_asins.csv"


def _read_asin_supplier_costs(csv_path: Path) -> dict[str, float | None]:
    """CSV (columns: asin, supplier_cost) -> {asin: supplier_cost}.

    An empty/missing `supplier_cost` cell becomes None (not 0) -- ROI can't
    be computed without a real supplier cost, and a silent 0 would make
    `compute_roi` treat it as "free," producing a fabricated ROI instead of
    admitting "unknown." See app/ingest.py's `_process_product` for how a
    None supplier_cost flows through as `computed_roi_pct = None`.
    """
    costs: dict[str, float | None] = {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asin = row["asin"].strip()
            raw_cost = (row.get("supplier_cost") or "").strip()
            costs[asin] = float(raw_cost) if raw_cost else None
    return costs


async def run_etl(csv_path: Path = DATA_CSV) -> list[dict]:
    await init_db()
    asin_costs = _read_asin_supplier_costs(csv_path)

    keepa_client = KeepaClient(api_keys=settings.keepa_api_keys_list)

    async with async_session_maker() as session:
        results = await fetch_and_upsert_batch(session, keepa_client, asin_costs)

    total = len(results)
    # The client batches internally at MAX_ASINS_PER_REQUEST ASINs/request
    # (see app/keepa/client.py) -- this mirrors that same math to report how
    # many real Keepa HTTP calls `fetch_and_upsert_batch`'s single logical
    # call actually made under the hood.
    calls = math.ceil(total / MAX_ASINS_PER_REQUEST) if total else 0
    ok = sum(1 for r in results if r["status"] == "ok")
    not_found = sum(1 for r in results if r["status"] == "not_found")
    error = sum(1 for r in results if r["status"] == "error")

    # HARNESS.md §4 greps logs for this exact phrase ("batched N ASINs in M
    # Keepa calls") -- keep the wording stable.
    logger.info(
        "batched %d ASINs in %d Keepa calls, %d ok / %d not_found / %d error",
        total,
        calls,
        ok,
        not_found,
        error,
    )
    for r in results:
        if r["status"] == "error":
            logger.warning("etl error for %s: %s", r["asin"], r["error"])

    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run_etl())


if __name__ == "__main__":
    main()
