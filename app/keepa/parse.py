"""Pure parsing/decoding helpers for Keepa's `/product` response shape.

No I/O here — everything is a plain function over dicts/numbers so it's
trivially unit-testable (see tests/test_keepa_parse.py) and reusable by
etl.py, the /upc and /eligibility routers, and the on-demand-fetch tool
without any of them needing to know Keepa's wire format.

Field-index reference (from KEEPA_QUICKSTART.md + the Keepa product-object
docs at https://keepa.com/#!discuss/t/product-object/116), applied to both
`csv[]` and `stats.current[]`:
    0  = AMAZON price history
    1  = NEW price history
    3  = SALES rank
    18 = BUY_BOX_SHIPPING

Sentinel handling: Keepa uses `-1` to mean "no data" (not zero) across
basically every numeric field. `safe_value()` centralizes that so `-1`
never silently becomes `0` in downstream math (eligibility rules, ROI).
"""
from datetime import datetime, timezone

AMAZON_SELLER_ID = "ATVPDKIKX0DER"

IDX_AMAZON = 0
IDX_NEW = 1
IDX_SALES_RANK = 3
IDX_BUY_BOX_SHIPPING = 18

# keepaTime -> unix seconds: (keepaMinutes + KEEPA_EPOCH_OFFSET_MIN) * 60.
# Per Keepa's docs, keepaTime is minutes since 2011-01-01 00:00:00 UTC minus
# a fixed offset baked into their format; KEEPA_QUICKSTART.md gives us the
# formula directly rather than making us derive the offset ourselves.
KEEPA_EPOCH_OFFSET_MIN = 21564000


def keepa_time_to_datetime(keepa_minutes: int) -> datetime:
    """Convert a Keepa-format minute timestamp to a tz-aware UTC datetime.

    Formula given verbatim in KEEPA_QUICKSTART.md:
        unix_seconds = (keepa_minutes + 21564000) * 60
    """
    unix_seconds = (keepa_minutes + KEEPA_EPOCH_OFFSET_MIN) * 60
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def safe_value(raw: int | float | None) -> float | None:
    """Treat Keepa's `-1` sentinel (and None) as "no data" -> None.

    Use this on every Keepa numeric field before doing any arithmetic on it,
    so `-1` never silently becomes `0` in ROI/eligibility math.
    """
    if raw is None:
        return None
    if raw == -1:
        return None
    return raw


def cents_to_dollars(cents: int | float | None) -> float | None:
    """Cents -> dollars, `-1`/None-safe (Keepa prices are always in cents)."""
    value = safe_value(cents)
    if value is None:
        return None
    return value / 100


def _stats_field(product: dict, field: str, index: int) -> int | float | None:
    """Fetch `product["stats"][field][index]`, tolerating a missing/short array.

    Handles two shapes Keepa uses for entries in `stats.*` arrays:
      - flat scalars (e.g. `current`, `avg90`): the price/rank/etc. sits
        directly at `[index]`.
      - `[time, value]` pairs (Keepa's `min`/`max` report *when* the
        extremum occurred alongside the value itself): the value we want is
        the last element of the pair, not the first.
    """
    stats = product.get("stats") or {}
    arr = stats.get(field)
    if not arr or len(arr) <= index:
        return None
    entry = arr[index]
    if isinstance(entry, (list, tuple)):
        return entry[-1] if entry else None
    return entry


def _stats_current(product: dict, index: int) -> int | float | None:
    """Fetch `product["stats"]["current"][index]`, tolerating missing keys."""
    return _stats_field(product, "current", index)


def extract_current_buybox(product: dict) -> float | None:
    """Current BuyBox price in dollars, from stats.current[BUY_BOX_SHIPPING].

    Only populated if the `/product` request was made with `stats=`/`buybox=1`
    — otherwise `product["stats"]` won't have a usable `current` array and
    this returns None (correct "no data", not a crash).
    """
    raw_cents = _stats_current(product, IDX_BUY_BOX_SHIPPING)
    return cents_to_dollars(raw_cents)


def extract_sales_rank(product: dict) -> int | None:
    """Current sales rank, from stats.current[SALES], `-1`-safe."""
    raw = _stats_current(product, IDX_SALES_RANK)
    value = safe_value(raw)
    return int(value) if value is not None else None


def extract_amazon_buybox_pct(product: dict) -> float | None:
    """Percentage of the observed window Amazon (ATVPDKIKX0DER) held the BuyBox.

    BEST-EFFORT / JUDGMENT CALL: Keepa's docs (per KEEPA_QUICKSTART.md's own
    admission) don't specify a formula for "Amazon's BuyBox share" — they only
    give the raw `buyBoxSellerIdHistory` field, described as
    `[keepaTimeStr, sellerId, keepaTimeStr, sellerId, ...]`. Interpretation
    used here, documented so a later phase can revisit it:

      1. Each entry `(t_i, seller_i)` means "seller_i held the BuyBox
         starting at time t_i, until the next entry's timestamp t_{i+1}".
      2. The *last* entry's holder is assumed to hold the box from t_last
         until "now" (time of the API call) — since a live product's BuyBox
         history is still open-ended, that seller's segment is included with
         a duration of `now - t_last` rather than being dropped.
      3. The overall window is `[t_0, now]`; the Amazon percentage is
         `sum(duration of segments where seller == ATVPDKIKX0DER) / total
         window duration * 100`.
      4. Timestamps in this field are Keepa-time strings/ints (minutes) per
         the same encoding as everywhere else, decoded via
         `keepa_time_to_datetime`.
      5. If the field is missing, empty, or has fewer than 2 entries (no full
         segment to weight), returns None rather than fabricating 0 — a
         single dangling entry doesn't tell us a duration.

    An alternative reading (weight by count of entries rather than duration)
    was considered and rejected: the task spec explicitly says "by duration,
    not just count of entries."
    """
    history = product.get("buyBoxSellerIdHistory")
    if not history or len(history) < 4:
        # Need at least 2 full (timestamp, seller) pairs to form one segment.
        return None

    pairs = []
    for i in range(0, len(history) - 1, 2):
        raw_ts = history[i]
        seller = history[i + 1]
        try:
            keepa_minutes = int(raw_ts)
        except (TypeError, ValueError):
            continue
        pairs.append((keepa_minutes, seller))

    if len(pairs) < 2:
        return None

    pairs.sort(key=lambda p: p[0])

    now = datetime.now(tz=timezone.utc)
    total_seconds = 0.0
    amazon_seconds = 0.0

    for i in range(len(pairs)):
        keepa_minutes, seller = pairs[i]
        start = keepa_time_to_datetime(keepa_minutes)
        if i + 1 < len(pairs):
            end = keepa_time_to_datetime(pairs[i + 1][0])
        else:
            end = now

        duration = (end - start).total_seconds()
        if duration <= 0:
            continue

        total_seconds += duration
        if seller == AMAZON_SELLER_ID:
            amazon_seconds += duration

    if total_seconds <= 0:
        return None

    return (amazon_seconds / total_seconds) * 100


def extract_referral_fee_pct(product: dict) -> float | None:
    """Referral fee percentage, `-1`-safe.

    Prefers `referralFeePercentage` (the current Product-struct field) over
    the deprecated `referralFeePercent` if both are present, per the field
    naming in the Keepa product object docs.
    """
    if "referralFeePercentage" in product:
        return safe_value(product.get("referralFeePercentage"))
    return safe_value(product.get("referralFeePercent"))


def extract_fba_pick_pack_cents(product: dict) -> int | None:
    """FBA pick-and-pack fee in cents, `-1`-safe, missing-key-safe."""
    fba_fees = product.get("fbaFees") or {}
    value = safe_value(fba_fees.get("pickAndPackFee"))
    return int(value) if value is not None else None


def extract_monthly_sold(product: dict) -> float | None:
    """Monthly units sold, if Keepa's response includes it directly.

    Keepa has no single canonical "monthly_sold" field across all response
    shapes; some product payloads include `monthlySold` directly. We use it
    when present and otherwise return None -- we deliberately do NOT
    estimate/derive a number from sales rank or anything else, since
    CHALLENGE.md's eligibility rule #5 depends on `monthly_sold` being a
    real, honest "no data" signal (null), not a guess.
    """
    return safe_value(product.get("monthlySold"))


def extract_avg90_buybox(product: dict) -> float | None:
    """90-day average BuyBox price in dollars, from stats.avg90[BUY_BOX_SHIPPING].

    Populated whenever the `/product` request included `stats=90` (this
    app's default -- see KeepaClient.get_products' `stats: int = 90`).
    `-1`/missing-safe like every other extractor here.
    """
    raw_cents = _stats_field(product, "avg90", IDX_BUY_BOX_SHIPPING)
    return cents_to_dollars(raw_cents)


def extract_min90_buybox(product: dict) -> float | None:
    """Minimum BuyBox price in dollars within the requested stats window,
    from stats.min[BUY_BOX_SHIPPING].

    JUDGMENT CALL (documented per this module's docstring convention):
    KEEPA_QUICKSTART.md doesn't spell out `stats.min`'s exact shape.
    Keepa's product-object docs describe it as a `[time, price]` pair per
    csv-type index (when the minimum occurred, and what it was) rather than
    a bare price like `avg90`. `_stats_field` handles both shapes
    defensively (takes the last element if it's a pair, the scalar
    otherwise), so this is correct either way the real API responds.
    """
    raw_cents = _stats_field(product, "min", IDX_BUY_BOX_SHIPPING)
    return cents_to_dollars(raw_cents)


def extract_number_of_items(product: dict) -> int | None:
    """Package quantity (e.g. a "6-pack" listing has numberOfItems=6),
    `-1`/missing-safe.

    Prefers `numberOfItems` (the field name CHALLENGE.md's glossary uses)
    and falls back to `packageQuantity`, which some Keepa payload shapes
    use for the same concept.
    """
    if "numberOfItems" in product:
        value = safe_value(product.get("numberOfItems"))
    else:
        value = safe_value(product.get("packageQuantity"))
    return int(value) if value is not None else None
