#!/usr/bin/env bash
# verify_refresh_resume.sh — HARNESS.md §8 acceptance script (the second of
# the two graded deliverables CHALLENGE.md's "交付清单" explicitly requires).
#
# Black-box script against the REAL running `docker compose` stack. This
# ACTUALLY kills and restarts the `worker` container mid-refresh -- it is not
# a simulation. Flow:
#   1. POST /refresh -> job_id, total
#   2. Poll GET /refresh/status until it's genuinely partway through
#      (0 < done+failed < total)
#   3. Capture (asin, updated_at) for a few ASINs that have already reached a
#      terminal per-item state (done OR failed) at that point
#   4. `docker compose kill worker` (simulates the worker process dying
#      mid-batch -- see app/tasks/refresh_tasks.py's module docstring for why
#      this leaves the job stuck at state='running' with no live consumer)
#   5. `docker compose up -d worker` (bring it back)
#   6. POST /refresh again -> must return the SAME job_id (no second job)
#   7. Poll to completion (state == done)
#   8. Assert the captured items' updated_at DIDN'T change (proof they were
#      NOT re-fetched) and done+failed == total
#
# Safe to run standalone, repeatedly: it never deletes data, only kills/
# restarts the `worker` service (never `db`/`broker`/`api`), and always
# leaves `worker` running again before exiting (checked explicitly at both
# the normal-exit and error-exit paths via a trap).
set -u

BASE_URL="${BASE_URL:-http://localhost:8000}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS_COUNT=0
FAIL_COUNT=0

_pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
_fail() { echo "  FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
_info() { echo "  INFO: $1"; }

_ensure_worker_up() {
    # Best-effort safety net: whatever happens above, leave `worker` running.
    echo "--- ensuring 'worker' service is up before exiting ---"
    (cd "$REPO_ROOT" && docker compose up -d worker) >/dev/null 2>&1
}
trap _ensure_worker_up EXIT

_status() {
    curl -s "$BASE_URL/refresh/status" -H "Authorization: Bearer $TOKEN"
}

_field() {
    # _field <json> <jq_expr>
    echo "$1" | jq -r "$2"
}

echo "=== verify_refresh_resume.sh — $BASE_URL ==="

echo "--- setup: register a throwaway user ---"
STAMP="$(date +%s)-$$"
EMAIL="refresh-verify-${STAMP}@example.com"
RESP=$(curl -s -X POST "$BASE_URL/auth/register" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"correct-horse-battery-staple\"}")
TOKEN=$(echo "$RESP" | jq -r .access_token 2>/dev/null)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not register a throwaway user. Response: $RESP"
    exit 2
fi
_info "registered $EMAIL"

echo "--- step 0: make sure we start from a clean slate (no job already running) ---"
DEADLINE=$(( $(date +%s) + 240 ))
while :; do
    ST=$(_status)
    STATE=$(_field "$ST" .state)
    if [ "$STATE" != "running" ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        _info "a prior job is still running after 240s of waiting -- proceeding anyway; "
        _info "this script's own POST /refresh will just fold into resuming it, which is "
        _info "itself a valid (if less clean) exercise of the same resume logic."
        break
    fi
    _info "a refresh job from before this script started is still running ($ST) -- waiting for it to finish first..."
    sleep 3
done

echo "--- step 1: POST /refresh ---"
T0=$(date +%s)
RESP=$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/refresh" -H "Authorization: Bearer $TOKEN")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
T1=$(date +%s)
ELAPSED=$((T1 - T0))
echo "  response (${ELAPSED}s): $BODY"

if [ "$HTTP_CODE" != "200" ]; then
    _fail "POST /refresh returned HTTP $HTTP_CODE, expected 200. Body: $BODY"
    exit 1
fi
JOB_ID=$(_field "$BODY" .job_id)
TOTAL=$(_field "$BODY" .total)
if [ -z "$JOB_ID" ] || [ "$JOB_ID" = "null" ]; then
    _fail "POST /refresh response had no job_id. Body: $BODY"
    exit 1
fi
_pass "POST /refresh returned job_id=$JOB_ID, state=running, total=$TOTAL"
if [ "$ELAPSED" -le 2 ]; then
    _pass "POST /refresh returned in ${ELAPSED}s (well under 1s target is ideal; comfortably fast either way, did NOT block on the whole batch)"
else
    _fail "POST /refresh took ${ELAPSED}s to return -- HARNESS.md §8 wants this back in ~1s, not blocking on the batch"
fi

echo "--- step 1b: second immediate POST /refresh returns the SAME job_id (reentrancy) ---"
RESP2=$(curl -s -X POST "$BASE_URL/refresh" -H "Authorization: Bearer $TOKEN")
JOB_ID_2=$(_field "$RESP2" .job_id)
if [ "$JOB_ID_2" = "$JOB_ID" ]; then
    _pass "back-to-back POST /refresh did not start a second job (same job_id=$JOB_ID)"
else
    _fail "back-to-back POST /refresh started a DIFFERENT job: $JOB_ID vs $JOB_ID_2"
fi

echo "--- step 2: poll /refresh/status until genuinely partway through ---"
DEADLINE=$(( $(date +%s) + 180 ))
DONE=0
FAILED=0
STATE="running"
while :; do
    ST=$(_status)
    STATE=$(_field "$ST" .state)
    DONE=$(_field "$ST" .done)
    FAILED=$(_field "$ST" .failed)
    SUM=$((DONE + FAILED))
    echo "  t+$(( $(date +%s) - T0 ))s: state=$STATE done=$DONE failed=$FAILED total=$TOTAL"
    if [ "$STATE" = "done" ]; then
        _info "job reached 'done' before we caught a partial window -- Keepa must be responding "
        _info "very fast in this environment. Falling back: will re-POST /refresh to start a "
        _info "fresh full job and try to catch a partial window on that one instead."
        break
    fi
    if [ "$SUM" -gt 0 ] && [ "$SUM" -lt "$TOTAL" ]; then
        _pass "caught the job genuinely partway through: done=$DONE failed=$FAILED total=$TOTAL"
        break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        _info "still at done=$DONE failed=$FAILED after 180s -- proceeding with whatever state we have"
        break
    fi
    sleep 1
done

if [ "$STATE" = "done" ]; then
    # Fallback: start a brand new job and try once more to catch a partial window.
    RESP=$(curl -s -X POST "$BASE_URL/refresh" -H "Authorization: Bearer $TOKEN")
    JOB_ID=$(_field "$RESP" .job_id)
    TOTAL=$(_field "$RESP" .total)
    _info "started fresh job_id=$JOB_ID total=$TOTAL for the retry"
    DEADLINE=$(( $(date +%s) + 180 ))
    while :; do
        ST=$(_status)
        STATE=$(_field "$ST" .state)
        DONE=$(_field "$ST" .done)
        FAILED=$(_field "$ST" .failed)
        SUM=$((DONE + FAILED))
        echo "  retry t+$(( $(date +%s) - T0 ))s: state=$STATE done=$DONE failed=$FAILED total=$TOTAL"
        if [ "$SUM" -gt 0 ] && [ "$SUM" -lt "$TOTAL" ]; then
            _pass "caught the RETRY job genuinely partway through: done=$DONE failed=$FAILED total=$TOTAL"
            break
        fi
        if [ "$STATE" = "done" ] || [ "$(date +%s)" -ge "$DEADLINE" ]; then
            _fail "could not catch a partial-progress window even on retry (Keepa responds too "
            _fail "fast/too uniformly in this environment to observe a mid-refresh state). The "
            _fail "kill/resume MECHANICS below will still be exercised, but the 'not redone' "
            _fail "updated_at proof needs a genuine in-flight item and cannot be fully validated "
            _fail "this run."
            break
        fi
        sleep 0.3
    done
fi

echo "--- step 3: capture (asin, updated_at) for a few items already in a terminal state ---"
CAPTURE_FILE=$(mktemp)
docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql -U keepa_scout -d keepa_scout -t -A -F',' -c \
    "SELECT asin, updated_at FROM refresh_job_items WHERE job_id='$JOB_ID' AND state IN ('done','failed') ORDER BY asin LIMIT 5" \
    > "$CAPTURE_FILE" 2>/dev/null
N_CAPTURED=$(grep -c . "$CAPTURE_FILE" || true)
echo "  captured $N_CAPTURED terminal item(s):"
cat "$CAPTURE_FILE" | sed 's/^/    /'

if [ "$N_CAPTURED" -gt 0 ]; then
    _pass "captured $N_CAPTURED already-terminal item(s) for the not-redone check"
else
    _fail "no terminal items captured before the kill -- the not-redone proof below will be vacuous"
fi

# Also record total refresh_jobs row count -- used later to prove the kill/
# resume didn't create a second job row.
JOBS_BEFORE=$(docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql -U keepa_scout -d keepa_scout -t -A -c "SELECT count(*) FROM refresh_jobs" 2>/dev/null | tr -d '[:space:]')
_info "refresh_jobs row count before kill/resume: $JOBS_BEFORE"

echo "--- step 4: docker compose kill worker (simulates the worker process dying mid-batch) ---"
(cd "$REPO_ROOT" && docker compose kill worker)
sleep 1
KILLED_STATE=$(docker compose -f "$REPO_ROOT/docker-compose.yml" ps worker --format '{{.State}}' 2>/dev/null)
_info "worker container state after kill: ${KILLED_STATE:-<not running>}"

echo "--- step 5: docker compose up -d worker ---"
(cd "$REPO_ROOT" && docker compose up -d worker)
sleep 3
UP_STATE=$(docker compose -f "$REPO_ROOT/docker-compose.yml" ps worker --format '{{.State}}' 2>/dev/null)
if [ "$UP_STATE" = "running" ]; then
    _pass "worker container is back up (state=running)"
else
    _fail "worker container did not come back up cleanly (state=${UP_STATE:-<unknown>})"
fi

echo "--- step 6: POST /refresh again -> must return the SAME job_id ---"
RESP=$(curl -s -X POST "$BASE_URL/refresh" -H "Authorization: Bearer $TOKEN")
RESUME_JOB_ID=$(_field "$RESP" .job_id)
echo "  response: $RESP"
if [ "$RESUME_JOB_ID" = "$JOB_ID" ]; then
    _pass "resumed the SAME job_id=$JOB_ID (not a new job)"
else
    _fail "resume returned a DIFFERENT job_id: expected $JOB_ID, got $RESUME_JOB_ID"
fi

JOBS_AFTER=$(docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql -U keepa_scout -d keepa_scout -t -A -c "SELECT count(*) FROM refresh_jobs" 2>/dev/null | tr -d '[:space:]')
_info "refresh_jobs row count after resume trigger: $JOBS_AFTER"
if [ "$JOBS_AFTER" = "$JOBS_BEFORE" ]; then
    _pass "no second refresh_jobs row was created by the resume ($JOBS_BEFORE -> $JOBS_AFTER)"
else
    _fail "refresh_jobs row count changed unexpectedly: $JOBS_BEFORE -> $JOBS_AFTER (a duplicate job may have been created)"
fi

echo "--- step 7: poll to completion ---"
DEADLINE=$(( $(date +%s) + 240 ))
while :; do
    ST=$(_status)
    STATE=$(_field "$ST" .state)
    DONE=$(_field "$ST" .done)
    FAILED=$(_field "$ST" .failed)
    echo "  t+$(( $(date +%s) - T0 ))s: state=$STATE done=$DONE failed=$FAILED total=$TOTAL"
    if [ "$STATE" = "done" ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        _fail "refresh did not reach state=done within 240s of the resume (stuck at done=$DONE failed=$FAILED)"
        break
    fi
    sleep 2
done

if [ "$STATE" = "done" ]; then
    _pass "refresh reached state=done"
    SUM=$((DONE + FAILED))
    if [ "$SUM" = "$TOTAL" ]; then
        _pass "done + failed == total ($DONE + $FAILED == $TOTAL)"
    else
        _fail "done + failed != total: $DONE + $FAILED != $TOTAL"
    fi
fi

echo "--- step 8: assert captured items' updated_at did NOT change (not re-fetched) ---"
if [ "$N_CAPTURED" -gt 0 ]; then
    ANY_CHANGED=0
    while IFS=',' read -r ASIN BEFORE_TS; do
        [ -z "$ASIN" ] && continue
        # NOTE: do NOT `tr -d '[:space:]'` here -- unlike the integer counts
        # elsewhere in this script, a Postgres timestamptz's text form has a
        # meaningful internal space between the date and time
        # ("2026-07-15 08:05:50.11+00"); stripping ALL whitespace collapses
        # it into "2026-07-1508:05:50.11+00" and makes an UNCHANGED
        # timestamp compare unequal to itself -- only trim the
        # leading/trailing whitespace psql's `-t -A` output may carry.
        AFTER_TS=$(docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
            psql -U keepa_scout -d keepa_scout -t -A -c \
            "SELECT updated_at FROM refresh_job_items WHERE job_id='$JOB_ID' AND asin='$ASIN'" 2>/dev/null \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        if [ "$AFTER_TS" = "$BEFORE_TS" ]; then
            _pass "$ASIN: updated_at unchanged ($BEFORE_TS) -- not re-fetched"
        else
            _fail "$ASIN: updated_at CHANGED ($BEFORE_TS -> $AFTER_TS) -- appears to have been re-fetched"
            ANY_CHANGED=1
        fi
    done < "$CAPTURE_FILE"
else
    _info "skipped (no items were captured in step 3)"
fi
rm -f "$CAPTURE_FILE"

echo
echo "=== verify_refresh_resume.sh summary: $PASS_COUNT passed, $FAIL_COUNT failed ==="

echo "--- final sanity: stack still healthy ---"
HEALTH_CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/health")
if [ "$HEALTH_CODE" = "200" ]; then
    _pass "GET /health -> 200 after this script's kill/restart"
else
    _fail "GET /health -> $HEALTH_CODE after this script's kill/restart (stack may not have recovered cleanly)"
fi
(cd "$REPO_ROOT" && docker compose ps)

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
