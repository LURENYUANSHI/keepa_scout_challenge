#!/usr/bin/env python3
"""verify_upc.py — HARNESS.md §2 acceptance script.

Black-box script against the REAL running `docker compose` stack (not a
pytest unit test / not a mocked Keepa response). Registers a throwaway user,
logs in, then hits the live `GET /upc` for every case in
`data/upc_test_cases.json`, printing PASS/FAIL per case plus a summary
table.

Stdlib only (urllib/json) — no `requests`/`httpx` dependency needed to run
this script from the host.

Keepa-unreachable handling (see this phase's brief): `/upc` calls Keepa
live and synchronously (app/routers/upc.py). If Keepa itself can't be
reached from wherever this runs, every variant attempt fails the same way
and the endpoint returns 502 (see upc.py's docstring: "Surface a clean 502
instead of letting an httpx/KeepaError traceback leak out"). That is an
*environment* problem, not a bug in the normalization logic under test, so
a 502 is reported as case status KEEPA_UNREACHABLE, tallied separately from
FAIL, and does not by itself make the script exit with the same code as a
real logic failure.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
UPC_CASES_PATH = REPO_ROOT / "data" / "upc_test_cases.json"

# case_06's all-9s input is the deliberately-fake barcode in
# data/upc_test_cases.json — no such product exists, so the CORRECT /upc
# behavior for it is an empty asins list. Without this, a correct empty
# result was graded FAIL and (via the any-FAIL exit path) failed the whole
# verify_all.sh run.
EXPECTED_EMPTY_INPUTS = {"999999999999"}


def _http(method: str, path: str, *, token: str | None = None, body: dict | None = None,
          timeout: float = 60.0) -> tuple[int, dict | str]:
    """Minimal stdlib HTTP helper. Returns (status_code, parsed_json_or_text)."""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        # OSError (not just URLError): a raw ConnectionResetError/
        # ConnectionRefusedError isn't always wrapped in a URLError by
        # urllib -- treat any transport-level failure as "unreachable"
        # (status 0) rather than letting it crash the script.
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _register_and_login() -> str:
    stamp = int(time.time())
    email = f"upc-verify-{stamp}@example.com"
    password = "correct-horse-battery-staple"
    status, body = _http(
        "POST", "/auth/register", body={"email": email, "password": password}
    )
    if status != 201:
        print(f"FATAL: could not register a throwaway user (HTTP {status}): {body}")
        sys.exit(2)
    assert isinstance(body, dict)
    token = body.get("access_token")
    if not token:
        print(f"FATAL: register response had no access_token: {body}")
        sys.exit(2)
    return token


def main() -> int:
    if not UPC_CASES_PATH.exists():
        print(f"FATAL: {UPC_CASES_PATH} not found.")
        return 2

    cases = json.loads(UPC_CASES_PATH.read_text())["cases"]

    print(f"=== verify_upc.py — {BASE_URL} — {len(cases)} case(s) from {UPC_CASES_PATH} ===")
    token = _register_and_login()
    print("Registered a throwaway user and obtained a token.\n")

    results = []
    for case in cases:
        case_id = case["id"]
        upc = case["input_upc"]
        required = case.get("required", True)
        status, body = _http("GET", f"/upc?upc={urllib.parse.quote(upc)}", token=token)

        if status == 502:
            outcome = "KEEPA_UNREACHABLE"
            asins = []
            detail = body.get("detail") if isinstance(body, dict) else str(body)
        elif status != 200:
            outcome = "FAIL"
            asins = []
            detail = f"HTTP {status}: {body}"
        else:
            assert isinstance(body, dict)
            asins = body.get("asins") or []
            if upc in EXPECTED_EMPTY_INPUTS:
                # The designed-fake barcode: the CORRECT behavior is an
                # empty list; finding "matches" for it would be the bug.
                outcome = "PASS" if not asins else "FAIL"
            else:
                outcome = "PASS" if asins else "FAIL"
            detail = f"normalized={body.get('normalized')}"

        results.append(
            {
                "id": case_id,
                "upc": upc,
                "required": required,
                "outcome": outcome,
                "asins": asins,
                "detail": detail,
            }
        )

        marker = {"PASS": "PASS", "FAIL": "FAIL", "KEEPA_UNREACHABLE": "SKIP"}[outcome]
        print(f"[{marker}] {case_id} (input={upc!r}, required={required}): "
              f"asins={asins} — {detail}")

    print("\n=== summary ===")
    print(f"{'case_id':<10} {'required':<9} {'outcome':<18} {'asins'}")
    for r in results:
        print(f"{r['id']:<10} {str(r['required']):<9} {r['outcome']:<18} {r['asins']}")

    n_pass = sum(1 for r in results if r["outcome"] == "PASS")
    n_fail = sum(1 for r in results if r["outcome"] == "FAIL")
    n_unreachable = sum(1 for r in results if r["outcome"] == "KEEPA_UNREACHABLE")
    required_fail = [r for r in results if r["outcome"] == "FAIL" and r["required"]]

    print(f"\n{n_pass} PASS / {n_fail} FAIL / {n_unreachable} KEEPA_UNREACHABLE "
          f"out of {len(results)} case(s).")

    if n_unreachable > 0:
        print(
            "\nNOTE: Keepa was unreachable from this environment for "
            f"{n_unreachable} case(s) (GET /upc returned 502 — see "
            "app/routers/upc.py's documented 502 path). This is an "
            "environment/connectivity issue, NOT evidence the UPC "
            "normalization logic (normalize_upc_variants) is wrong -- those "
            "cases could not be validated either way. Re-run this script "
            "somewhere with live Keepa connectivity to get a real PASS/FAIL "
            "verdict on them."
        )

    if required_fail:
        print(
            f"\n{len(required_fail)} REQUIRED case(s) got a real (non-Keepa-outage) "
            f"FAIL: {[r['id'] for r in required_fail]}"
        )
        return 1  # a genuine logic failure -> exit 1

    if n_unreachable > 0 and n_fail == 0:
        # Nothing definitively failed, but we couldn't fully validate either
        # -- distinct exit code so callers can tell "inconclusive" apart
        # from both "all good" (0) and "proven broken" (1).
        return 2

    if n_fail > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
