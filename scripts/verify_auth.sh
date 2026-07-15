#!/usr/bin/env bash
# verify_auth.sh — HARNESS.md §1 acceptance script.
#
# Black-box script against the REAL running `docker compose` stack (not
# pytest). Exercises, via curl against a live :8000:
#   - POST /auth/register: success issues a usable token; duplicate email -> 409
#   - POST /auth/login: wrong password -> 401; correct password -> 200 + new token
#   - a protected route with no token -> 401
#   - a protected route with a valid token -> 200
#   - cross-user session_id access -> 403 (HARNESS.md §1's "不能靠猜 session_id
#     越权读写" bullet, also in its 证据 block even though not restated in
#     this phase's task summary)
#
# Every assertion prints PASS/FAIL. Exits non-zero if anything unexpected
# happened.
set -u

BASE_URL="${BASE_URL:-http://localhost:8000}"
PASS_COUNT=0
FAIL_COUNT=0

_pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
_fail() { echo "  FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

_assert_status() {
    # _assert_status <description> <expected_http_code> <actual_http_code> <body>
    local desc="$1" expected="$2" actual="$3" body="$4"
    if [ "$actual" = "$expected" ]; then
        _pass "$desc (HTTP $actual)"
    else
        _fail "$desc — expected HTTP $expected, got HTTP $actual. Body: $body"
    fi
}

# curl helper: prints "<body>\n<http_code>" so we can split both out.
_req() {
    curl -s -o /tmp/verify_auth_body.$$ -w "%{http_code}" "$@"
}

echo "=== verify_auth.sh — $BASE_URL ==="
STAMP="$(date +%s)-$$"
EMAIL_A="auth-verify-a-${STAMP}@example.com"
EMAIL_B="auth-verify-b-${STAMP}@example.com"
PASSWORD="correct-horse-battery-staple"
WRONG_PASSWORD="definitely-not-the-password"

echo "--- 1. POST /auth/register (user A, new email) -> 201 + usable token ---"
CODE=$(_req -X POST "$BASE_URL/auth/register" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL_A\",\"password\":\"$PASSWORD\"}")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "register user A" 201 "$CODE" "$BODY"
TOKEN_A=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$TOKEN_A" ]; then
    _pass "register response included a non-empty access_token"
else
    _fail "register response missing access_token. Body: $BODY"
fi

echo "--- 2. POST /auth/register (same email again) -> 409 ---"
CODE=$(_req -X POST "$BASE_URL/auth/register" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL_A\",\"password\":\"$PASSWORD\"}")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "duplicate-email register" 409 "$CODE" "$BODY"

echo "--- 3. POST /auth/login (wrong password) -> 401 ---"
CODE=$(_req -X POST "$BASE_URL/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL_A\",\"password\":\"$WRONG_PASSWORD\"}")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "wrong-password login" 401 "$CODE" "$BODY"

echo "--- 4. POST /auth/login (correct password) -> 200 + new token ---"
CODE=$(_req -X POST "$BASE_URL/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL_A\",\"password\":\"$PASSWORD\"}")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "correct-password login" 200 "$CODE" "$BODY"
LOGIN_TOKEN_A=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$LOGIN_TOKEN_A" ]; then
    _pass "login response included a non-empty access_token"
else
    _fail "login response missing access_token. Body: $BODY"
fi
if [ -n "$LOGIN_TOKEN_A" ] && [ "$LOGIN_TOKEN_A" != "$TOKEN_A" ]; then
    _pass "login issued a DIFFERENT token than register (a fresh token, not a cached one)"
else
    _fail "login token was identical to (or missing vs) the register token: '$LOGIN_TOKEN_A' vs '$TOKEN_A'"
fi

echo "--- 5. Protected route with NO token -> 401 ---"
CODE=$(_req "$BASE_URL/auth/_whoami")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "protected route, no Authorization header" 401 "$CODE" "$BODY"

echo "--- 5b. Protected route with a garbage token -> 401 ---"
CODE=$(_req "$BASE_URL/auth/_whoami" -H "Authorization: Bearer not-a-real-token")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "protected route, garbage token" 401 "$CODE" "$BODY"

echo "--- 6. Protected route WITH a valid token -> 200 ---"
CODE=$(_req "$BASE_URL/auth/_whoami" -H "Authorization: Bearer $LOGIN_TOKEN_A")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "protected route, valid token" 200 "$CODE" "$BODY"

echo "--- 7. Cross-user session_id access -> 403 ---"
echo "    (register user B, have A create a chat session, then B tries to post to it)"
CODE=$(_req -X POST "$BASE_URL/auth/register" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL_B\",\"password\":\"$PASSWORD\"}")
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "register user B" 201 "$CODE" "$BODY"
TOKEN_B=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

SESSION_ID="auth-verify-session-${STAMP}"
CODE=$(_req -X POST "$BASE_URL/chat" -H "Authorization: Bearer $LOGIN_TOKEN_A" \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"What does ROI mean?\"}" \
    --max-time 60)
BODY=$(cat /tmp/verify_auth_body.$$)
if [ "$CODE" = "200" ]; then
    _pass "user A created/used chat session '$SESSION_ID' (HTTP 200)"
else
    _fail "user A's own /chat call unexpectedly returned HTTP $CODE — cannot continue cross-user check. Body: $BODY"
fi

CODE=$(_req -X POST "$BASE_URL/chat" -H "Authorization: Bearer $TOKEN_B" \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"hi\"}" \
    --max-time 60)
BODY=$(cat /tmp/verify_auth_body.$$)
_assert_status "user B accessing user A's session_id" 403 "$CODE" "$BODY"

rm -f /tmp/verify_auth_body.$$

echo
echo "=== verify_auth.sh summary: $PASS_COUNT passed, $FAIL_COUNT failed ==="
if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
