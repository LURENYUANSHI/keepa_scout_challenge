"""POST /auth/register, POST /auth/login, and the get_current_user dependency.

See ARCHITECTURE.md §3.1 / §3.2 / §6 and HARNESS.md §1 for the behavior
these tests are pinning down. GET /auth/_whoami is a throwaway route (see
app/routers/auth.py) that exists purely so the auth dependency can be
smoke-tested end-to-end before any real protected endpoint exists.
"""
import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


async def test_register_success_returns_usable_token(client):
    email = _unique_email()
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"]
    assert body["expires_at"]

    # The token is immediately usable against a protected route.
    whoami = await client.get(
        "/auth/_whoami",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert whoami.status_code == 200
    assert "user_id" in whoami.json()


async def test_register_duplicate_email_conflict(client):
    email = _unique_email()
    first = await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )
    assert first.status_code == 201

    second = await client.post(
        "/auth/register", json={"email": email, "password": "another password"}
    )
    assert second.status_code == 409

    # Case-insensitive dedupe (lower(email)) — ARCHITECTURE.md §6.
    third = await client.post(
        "/auth/register",
        json={"email": email.upper(), "password": "another password"},
    )
    assert third.status_code == 409


async def test_register_password_too_short(client):
    resp = await client.post(
        "/auth/register", json={"email": _unique_email(), "password": "short1"}
    )
    assert resp.status_code == 400
    assert "short" in resp.json()["detail"].lower()


async def test_register_password_too_long(client):
    resp = await client.post(
        "/auth/register",
        json={"email": _unique_email(), "password": "x" * 73},
    )
    assert resp.status_code == 400
    assert "long" in resp.json()["detail"].lower()


async def test_login_wrong_password(client):
    email = _unique_email()
    register = await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )
    assert register.status_code == 201

    resp = await client.post(
        "/auth/login", json={"email": email, "password": "wrong password"}
    )
    assert resp.status_code == 401
    assert "detail" in resp.json()


async def test_login_unknown_email_same_status_and_message_as_wrong_password(client):
    email = _unique_email()
    await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )

    wrong_password_resp = await client.post(
        "/auth/login", json={"email": email, "password": "wrong password"}
    )
    unknown_email_resp = await client.post(
        "/auth/login",
        json={"email": _unique_email(), "password": "whatever password"},
    )

    assert wrong_password_resp.status_code == 401
    assert unknown_email_resp.status_code == 401
    # Same status AND same message — don't leak which emails are registered.
    assert wrong_password_resp.json()["detail"] == unknown_email_resp.json()["detail"]


async def test_login_success_issues_new_token(client):
    email = _unique_email()
    register = await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )
    register_token = register.json()["access_token"]

    login = await client.post(
        "/auth/login", json={"email": email, "password": "correct horse"}
    )
    assert login.status_code == 200
    login_token = login.json()["access_token"]

    # Login issues a distinct token from the auto-login one at registration.
    assert login_token != register_token

    whoami = await client.get(
        "/auth/_whoami", headers={"Authorization": f"Bearer {login_token}"}
    )
    assert whoami.status_code == 200


async def test_whoami_no_token(client):
    resp = await client.get("/auth/_whoami")
    assert resp.status_code == 401


async def test_whoami_garbage_token(client):
    resp = await client.get(
        "/auth/_whoami", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401


async def test_whoami_malformed_header(client):
    resp = await client.get(
        "/auth/_whoami", headers={"Authorization": "not-even-bearer-formatted"}
    )
    assert resp.status_code == 401


async def test_whoami_valid_token(client):
    email = _unique_email()
    register = await client.post(
        "/auth/register", json={"email": email, "password": "correct horse"}
    )
    token = register.json()["access_token"]

    resp = await client.get(
        "/auth/_whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"]
