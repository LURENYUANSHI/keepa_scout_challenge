"""Tests for app/routers/upc.py's `normalize_upc_variants()` + the full
`GET /upc` endpoint, against the actual 7 inputs in
data/upc_test_cases.json.

All Keepa HTTP calls are mocked via respx -- this suite makes zero real
calls to api.keepa.com (Keepa tokens are precious, and per this project's
notes a prior phase found this sandbox may not even reach the real API at
all). For each case:
  1. `test_normalize_upc_variants_matches_documented_rules` asserts
     `normalize_upc_variants()` produces the exact variant sequence our
     documented rules predict (see app/routers/upc.py's docstring),
     independent of any live call.
  2. A per-case endpoint test drives that sequence through a mocked Keepa
     (every variant returns empty EXCEPT the one we predict is "correct",
     which returns a product) via the real `GET /upc` route, proving the
     router's "try each variant in order until one succeeds" loop is wired
     correctly end to end.
"""
import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from app.routers.upc import normalize_upc_variants

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here (unlike
# tests/test_auth.py) because this file mixes sync tests
# (test_normalize_upc_variants_matches_documented_rules, pure functions, no
# I/O) with async ones (the endpoint tests) -- pytest.ini's
# `asyncio_mode = auto` already auto-detects `async def` tests, so no
# marker is needed either way; applying one indiscriminately just warns on
# the sync tests.

CASES_PATH = Path(__file__).resolve().parent.parent / "data" / "upc_test_cases.json"
CASES = {c["id"]: c["input_upc"] for c in json.loads(CASES_PATH.read_text())["cases"]}

# Hand-verified against normalize_upc_variants()'s documented rules -- see
# app/routers/upc.py's docstring for the reasoning behind each transform.
EXPECTED_VARIANTS = {
    "case_01": ["070537500052"],  # trivial: already a clean 12-digit UPC-A
    "case_02": ["70537500052", "070537500052"],  # 11 -> zero-pad to 12
    "case_03": ["9780545465298", "780545465298"],  # 13 -> strip leading digit
    "case_04": ["00000772041997", "0000772041997", "000772041997"],  # 14 -> strip 1, strip 2
    "case_05": ["052144100245"],  # already 12 digits
    "case_06": ["999999999999"],  # already 12 digits, no real product behind it
    "case_07": ["070537500052"],  # dashes stripped -> same as case_01
}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_normalize_upc_variants_matches_documented_rules(case_id):
    raw = CASES[case_id]
    assert normalize_upc_variants(raw) == EXPECTED_VARIANTS[case_id]


async def _register(client) -> str:
    resp = await client.post(
        "/auth/register",
        json={"email": f"upc-{uuid.uuid4().hex}@example.com", "password": "correct horse"},
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


def _mock_code_route(success_variant: str, asins: list[str]) -> None:
    """Every `code=` variant returns empty EXCEPT `success_variant`, which
    returns `asins` -- mirrors "mock: original variant returns empty, but
    your predicted correct variant returns a result."""

    def _responder(request: httpx.Request) -> httpx.Response:
        code = request.url.params["code"]
        if code == success_variant:
            return httpx.Response(
                200,
                json={
                    "tokensLeft": 100,
                    "products": [{"asin": a, "title": f"Product {a}"} for a in asins],
                },
            )
        return httpx.Response(200, json={"tokensLeft": 100, "products": []})

    respx.get("https://api.keepa.com/product").mock(side_effect=_responder)


@respx.mock
async def test_case_01_trivial_no_normalization_needed(client):
    token = await _register(client)
    _mock_code_route("070537500052", ["B0000021VO"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_01"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["input"] == CASES["case_01"]
    assert body["normalized"] == ["070537500052"]
    assert body["asins"] == ["B0000021VO"]


@respx.mock
async def test_case_02_easy_leading_zero_padding(client):
    token = await _register(client)
    # Original 11-digit form returns nothing; the zero-padded 12-digit form succeeds.
    _mock_code_route("070537500052", ["B0000021VO"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_02"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["70537500052", "070537500052"]
    assert body["asins"] == ["B0000021VO"]


@respx.mock
async def test_case_03_medium_ean13_strip_leading_digit(client):
    token = await _register(client)
    # NOTE (see app/routers/upc.py's documented caveat): the generic
    # "13-digit -> strip leading digit" rule is a known-imperfect fit for
    # this Bookland/ISBN-prefixed input. This test only proves our
    # *mechanical* rule fires and the endpoint tries the resulting variant
    # -- it is not a claim that "780545465298" is what real Keepa resolves.
    _mock_code_route("780545465298", ["B0000ISBN01"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_03"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["9780545465298", "780545465298"]
    assert body["asins"] == ["B0000ISBN01"]


@respx.mock
async def test_case_04_hard_gtin14_strip_two_digits(client):
    token = await _register(client)
    _mock_code_route("000772041997", ["B0000GTIN01"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_04"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["00000772041997", "0000772041997", "000772041997"]
    assert body["asins"] == ["B0000GTIN01"]


@respx.mock
async def test_case_05_medium_multi_asin_result_returns_all(client):
    token = await _register(client)
    # Works as-is; this is the "one UPC -> multiple ASINs" scenario
    # (HARNESS.md §2: "已知对应多个 ASIN 的 UPC，返回数组长度 > 1").
    _mock_code_route("052144100245", ["B0PACK0001", "B0PACK0012"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_05"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["052144100245"]
    assert len(body["asins"]) > 1
    assert set(body["asins"]) == {"B0PACK0001", "B0PACK0012"}


@respx.mock
async def test_case_06_easy_no_variant_matches_returns_empty_gracefully(client):
    token = await _register(client)
    # Nothing succeeds for any variant -- not required (difficulty=easy,
    # required=false), but the endpoint must still respond cleanly with an
    # empty `asins` list, not error out.
    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(200, json={"tokensLeft": 100, "products": []})
    )

    resp = await client.get(
        "/upc", params={"upc": CASES["case_06"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["999999999999"]
    assert body["asins"] == []


@respx.mock
async def test_case_07_easy_strips_dashes(client):
    token = await _register(client)
    _mock_code_route("070537500052", ["B0000021VO"])

    resp = await client.get(
        "/upc", params={"upc": CASES["case_07"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized"] == ["070537500052"]
    assert body["asins"] == ["B0000021VO"]


async def test_upc_requires_auth(client):
    # No respx mock needed here -- auth must fail before any Keepa call.
    resp = await client.get("/upc", params={"upc": "070537500052"})
    assert resp.status_code == 401
