#!/usr/bin/env python3
"""cost_report.py — HARNESS.md §9 acceptance script.

Two things, both read-only:
  1. Sums `llm_usage_log` (input/output/total tokens, call count) -- overall
     and broken down by endpoint/model -- via a direct psql query against the
     running `db` container (no app code import needed).
  2. Calls Keepa's free `GET /token` endpoint directly for the real key
     balance (tokensLeft/refillRate) for every configured Keepa API key.

Deliberately no frontend/dashboard (HARNESS.md §9: "明确不做前端
dashboard/图表 -- 一条脚本输出到终端就够"). Stdlib only.

Keepa-unreachable handling: if Keepa can't be reached from wherever this
runs, that's reported as "unreachable" per key rather than crashing the
whole report -- the LLM usage half of the report is independent of Keepa
and should still print even if Keepa is down.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KEEPA_BASE_URL = "https://api.keepa.com"


def _read_env_file() -> dict[str, str]:
    """Parse `.env` (KEY=VALUE lines, '#' comments) without needing
    python-dotenv installed on the host -- this script runs outside the
    container, so app.config.settings isn't importable without the app's
    own dependency set."""
    env_path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _psql(sql: str) -> str:
    """Run a SQL statement against the `db` compose service via `docker
    compose exec`, return raw stdout. Uses -t -A -F for pipe-delimited,
    unaligned, tuples-only output that's easy to parse."""
    proc = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "db",
            "psql", "-U", "keepa_scout", "-d", "keepa_scout",
            "-t", "-A", "-F", "|",
            "-c", sql,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _print_llm_usage_report() -> None:
    print("=== LLM token usage (llm_usage_log) ===")

    try:
        totals_raw = _psql(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(total_tokens),0), COUNT(*) FROM llm_usage_log"
        ).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not query llm_usage_log (is `docker compose up db` running?): {exc}")
        return

    if not totals_raw:
        print("  No rows in llm_usage_log yet (no /chat calls have been made).")
        return

    input_tok, output_tok, total_tok, n_calls = totals_raw.split("|")
    print(
        f"  LLM: {int(input_tok):,} input / {int(output_tok):,} output / "
        f"{int(total_tok):,} total tokens across {int(n_calls):,} call(s)"
    )

    print("\n  --- breakdown by endpoint / model ---")
    breakdown_raw = _psql(
        "SELECT endpoint, model, COUNT(*), COALESCE(SUM(input_tokens),0), "
        "COALESCE(SUM(output_tokens),0), COALESCE(SUM(total_tokens),0) "
        "FROM llm_usage_log GROUP BY endpoint, model ORDER BY endpoint, model"
    ).strip()
    if breakdown_raw:
        print(f"  {'endpoint':<14} {'model':<20} {'calls':>7} {'input':>10} {'output':>10} {'total':>10}")
        for line in breakdown_raw.splitlines():
            endpoint, model, calls, inp, outp, tot = line.split("|")
            print(f"  {endpoint:<14} {model:<20} {int(calls):>7} {int(inp):>10,} {int(outp):>10,} {int(tot):>10,}")

    print("\n  --- breakdown by user (top 10 by total tokens) ---")
    by_user_raw = _psql(
        "SELECT u.email, COUNT(*), COALESCE(SUM(l.total_tokens),0) "
        "FROM llm_usage_log l JOIN users u ON u.id = l.user_id "
        "GROUP BY u.email ORDER BY 3 DESC LIMIT 10"
    ).strip()
    if by_user_raw:
        for line in by_user_raw.splitlines():
            email, calls, tot = line.split("|")
            print(f"  {email:<40} {int(calls):>5} calls  {int(tot):>8,} tokens")


def _keepa_token_status(api_key: str) -> dict:
    url = f"{KEEPA_BASE_URL}/token?key={api_key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            # Keepa gzips essentially every response regardless of
            # Accept-Encoding, and urllib does NOT auto-decompress — the
            # 0x8b in the old "'utf-8' codec can't decode byte 0x8b" error
            # was the gzip magic number, i.e. Keepa WAS reachable.
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return {"ok": True, "body": json.loads(raw.decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"unreachable: {exc.reason}"}
    except OSError as exc:
        # A raw ConnectionResetError/ConnectionRefusedError isn't always
        # wrapped in a URLError by urllib -- catch it explicitly too.
        return {"ok": False, "error": f"unreachable: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _print_keepa_token_report() -> None:
    print("\n=== Keepa token balance (GET /token, free, no token cost) ===")

    env = {**_read_env_file(), **os.environ}
    raw_keys = env.get("KEEPA_API_KEYS", "")
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

    if not keys:
        print("  No KEEPA_API_KEYS configured (.env) -- nothing to check.")
        return

    for i, key in enumerate(keys, start=1):
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
        result = _keepa_token_status(key)
        if result["ok"]:
            body = result["body"]
            tokens_left = body.get("tokensLeft")
            print(f"  key #{i} ({masked}): tokensLeft={tokens_left}, "
                  f"refillIn={body.get('refillIn')}, refillRate={body.get('refillRate')}, "
                  f"tokenFlowReduction={body.get('tokenFlowReduction')}")
            print(f"      raw response: {json.dumps(body)}")
        else:
            print(f"  key #{i} ({masked}): UNREACHABLE — {result['error']}")

    print(
        "\n  NOTE: if every key above is UNREACHABLE, Keepa itself could not be reached from "
        "wherever this script is running (network/DNS/firewall) -- this is an environment "
        "issue, not a bug in this script or in the app's Keepa client."
    )


def main() -> int:
    print("=== cost_report.py — Keepa Scout cost accounting ===\n")
    _print_llm_usage_report()
    _print_keepa_token_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
