"""GET /upc — UPC/EAN -> ASIN lookup via Keepa's `/product?code=`, with
normalization + multi-variant retry.

Unlike /eligibility, this DOES call Keepa live and synchronously (per
ARCHITECTURE.md §1: "/upc、/eligibility 这种单次查询由 api 直接同步调 Keepa
（延迟可接受，不需要排队)" — /upc is the half of that sentence that's
actually true; see app/routers/eligibility.py's docstring for why
/eligibility itself stayed DB-only).

Normalization strategy (mechanical, deliberately simple -- see
`normalize_upc_variants`'s docstring for the exact rules and one documented
caveat):
  1. Strip every non-digit character from the raw input.
  2. Try the cleaned code as-is against Keepa first.
  3. If that returns zero products, try each length-based "standard barcode
     variant" in turn until one returns a non-empty result, or the variant
     list is exhausted.
ALL matching ASINs from whichever variant first succeeds are returned, not
just the first — CHALLENGE.md: "同一个 UPC 在 Amazon 可能对应多个 listing
... 全部返回，不要只取第一个", and HARNESS.md §2 checks for `len(asins) > 1`
on the known multi-ASIN case.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import get_current_user
from app.keepa.client import KeepaClient, get_keepa_client
from app.models.user import User

router = APIRouter(tags=["upc"])


def normalize_upc_variants(raw: str) -> list[str]:
    """Digits-only `raw` input -> an ordered list of candidate codes to try
    against Keepa's `code=` param, cleaned-original first.

    Rules, by length of the digits-only string:
      - 11 digits: likely a UPC-A that lost its leading check/system digit
        in transit (e.g. a spreadsheet dropped a leading zero) -> zero-pad
        back to 12.
      - 13 digits: likely a UPC-A re-encoded as EAN-13 (EAN-13 encodes a
        UPC-A as "0" + the 12 UPC-A digits) -> strip the leading digit to
        get back to a 12-digit code.
        CAVEAT (intentionally not handled, documented rather than silently
        claimed as covered): this heuristic is specifically wrong for
        Bookland/ISBN EAN-13s (978/979 prefix, e.g.
        `data/upc_test_cases.json`'s case_03) -- those aren't "a UPC-A with
        an extra leading 0", they're a real ISBN-13 whose Amazon-side
        identity is the corresponding 10-digit ISBN (a completely different
        transform: drop the 3-digit "978"/"979" prefix and the check digit,
        then recompute an ISBN-10 check digit). We deliberately don't
        special-case that conversion here — it's flagged as a known gap
        rather than an untested guess dressed up as a real feature.
      - 14 digits: a GTIN-14, which is 1 packaging-level indicator digit
        prepended to a 13-or-12-digit code -> try stripping 1 digit (-> 13)
        and 2 digits (-> 12).
      - Anything else (already 12 digits, or an unrecognized length): only
        the as-is form is tried.
    Bonus, not required by any rule above but free to compute and harmless:
    also try stripping ALL leading zeros and re-padding to 12 -- this covers
    inputs with *more* than one spurious leading zero (e.g. the "hard"/
    optional case_04 in `data/upc_test_cases.json`) that the length-specific
    rules above don't reach.
    """
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return []

    variants = [digits]
    n = len(digits)

    if n == 11:
        variants.append(digits.zfill(12))
    elif n == 13:
        variants.append(digits[1:])
    elif n == 14:
        variants.append(digits[1:])
        variants.append(digits[2:])

    stripped = digits.lstrip("0")
    if stripped and stripped != digits and len(stripped) <= 12:
        variants.append(stripped.zfill(12))

    seen: set[str] = set()
    ordered: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered


@router.get("/upc")
async def lookup_upc(
    upc: str,
    keepa_client: KeepaClient = Depends(get_keepa_client),
    user: User = Depends(get_current_user),
) -> dict:
    variants = normalize_upc_variants(upc)

    tried: list[str] = []
    asins: list[str] = []
    for variant in variants:
        tried.append(variant)
        try:
            response = await keepa_client.get_products(code=variant)
        except Exception as exc:
            # Keepa itself is unreachable/erroring (network down, rate
            # limited, all keys exhausted, ...) -- this affects every
            # variant equally (it's not "this one code is bad"), so retrying
            # the rest against the same broken connection is pointless.
            # Surface a clean 502 instead of letting an httpx/KeepaError
            # traceback leak out as an unhandled 500.
            # str(exc) can be empty for some httpx exceptions (e.g.
            # ConnectTimeout carries no message) -- fall back to the
            # exception's class name so the detail is never blank.
            reason = str(exc) or type(exc).__name__
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Keepa lookup failed ({variant!r}): {reason}",
            ) from exc
        products = [p for p in (response.get("products") or []) if p]
        found_asins = [p["asin"] for p in products if p.get("asin")]
        if found_asins:
            asins = found_asins
            break

    return {"input": upc, "normalized": tried, "asins": asins}
