#!/usr/bin/env bash
# verify_all.sh — HARNESS.md's top-level "跑这份 harness" entry point.
#
# Orchestrates every independent acceptance script in this directory in
# sequence and aggregates pass/fail. Does NOT reimplement any verification
# logic itself -- it only calls the other scripts and inspects their exit
# codes, per HARNESS.md: "verify_all.sh 是所有独立验收脚本的编排入口，不重复
# 实现校验逻辑，只负责按顺序调用 + 汇总退出码".
#
# Usage:
#   docker compose up --build -d
#   ./scripts/verify_all.sh
#
# Exit code: 0 if every script passed, non-zero if any failed.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8000}"
export BASE_URL

# python3.11+ if available (repo targets 3.11+ per CHALLENGE.md), falling
# back to whatever `python3` resolves to on this host.
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN=python3.11
    else
        PYTHON_BIN=python3
    fi
fi

declare -a NAMES
declare -a STATUSES

_run() {
    local name="$1"
    shift
    echo
    echo "############################################################"
    echo "# $name"
    echo "############################################################"
    "$@"
    local code=$?
    NAMES+=("$name")
    STATUSES+=("$code")
    return 0
}

echo "=== verify_all.sh — $BASE_URL ==="
echo "Waiting for GET /health to return 200 before starting..."
DEADLINE=$(( $(date +%s) + 60 ))
until curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/health" | grep -q '^200$'; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "FATAL: $BASE_URL/health never returned 200 within 60s. Is the stack up "
        echo "('docker compose up -d db broker api worker')?"
        exit 2
    fi
    sleep 1
done
echo "API is healthy."

_run "1. verify_auth.sh"           bash "$SCRIPT_DIR/verify_auth.sh"
_run "2. verify_upc.py"            "$PYTHON_BIN" "$SCRIPT_DIR/verify_upc.py"
_run "3. verify_chat.py"           "$PYTHON_BIN" "$SCRIPT_DIR/verify_chat.py"
_run "4. verify_refresh_resume.sh" bash "$SCRIPT_DIR/verify_refresh_resume.sh"
_run "5. cost_report.py"           "$PYTHON_BIN" "$SCRIPT_DIR/cost_report.py"

echo
echo "############################################################"
echo "# verify_all.sh — final summary"
echo "############################################################"
printf "%-32s %-10s\n" "SCRIPT" "RESULT"
printf "%-32s %-10s\n" "------" "------"

OVERALL=0
for i in "${!NAMES[@]}"; do
    name="${NAMES[$i]}"
    code="${STATUSES[$i]}"
    if [ "$code" -eq 0 ]; then
        result="PASS"
    elif [ "$name" = "2. verify_upc.py" ] && [ "$code" -eq 2 ]; then
        # verify_upc.py's distinct exit code 2 == "inconclusive, Keepa
        # unreachable" -- not a proven logic failure. Reported distinctly,
        # doesn't flip verify_all.sh's overall result to a hard FAIL, but is
        # NOT silently treated as a clean pass either.
        result="INCONCLUSIVE (Keepa unreachable, code 2)"
    else
        result="FAIL (exit $code)"
        OVERALL=1
    fi
    printf "%-32s %-10s\n" "$name" "$result"
done

echo
if [ "$OVERALL" -eq 0 ]; then
    echo "verify_all.sh: ALL SCRIPTS PASSED (or were inconclusive only due to Keepa connectivity)."
else
    echo "verify_all.sh: AT LEAST ONE SCRIPT FAILED."
fi

exit "$OVERALL"
