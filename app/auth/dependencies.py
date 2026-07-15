"""get_current_user — the unified auth dependency from ARCHITECTURE.md §3.2.

Every protected route (later phases: /upc, /eligibility, /ask, /chat,
/refresh) is expected to depend on this via `Depends(get_current_user)` to
get back a `User` ORM instance (`.id`, `.email`, ...). It reads the
`Authorization: Bearer <token>` header, hashes the token, and looks it up in
`auth_tokens`, requiring `revoked_at IS NULL AND expires_at > now()`.

`get_user_by_token()` below is the same lookup, factored out to take a raw
token string instead of a `Request`/`Header` -- added for `WS /chat/stream`
(app/routers/chat.py), which can't rely on FastAPI's `Header(...)` the way
HTTP routes do: a browser `new WebSocket(url)` call cannot set custom
request headers, so that endpoint authenticates via a `?token=` query param
instead and needs to run this exact lookup against a raw string. Returns
`None` on any failure instead of raising, so the WS caller can decide how to
close the connection (HTTPException doesn't map onto a WS handshake the same
way it does an HTTP response).
"""
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_token
from app.db import get_db
from app.models.user import AuthToken, User


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_user_by_token(token: str | None, db: AsyncSession) -> User | None:
    """Same lookup `get_current_user` does, minus the `Authorization:
    Bearer ...` header parsing -- takes the raw token string directly.
    Returns `None` (never raises) for: missing/blank token, unknown token,
    revoked token, expired token, or an orphaned token row -- all
    indistinguishable to the caller, same as `get_current_user`."""
    if not token or not token.strip():
        return None

    token_hash = hash_token(token.strip())

    result = await db.execute(
        select(AuthToken).where(
            AuthToken.token_hash == token_hash,
            AuthToken.revoked_at.is_(None),
            AuthToken.expires_at > datetime.now(timezone.utc),
        )
    )
    auth_token = result.scalar_one_or_none()
    if auth_token is None:
        return None

    return await db.get(User, auth_token.user_id)


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized()

    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise _unauthorized()

    user = await get_user_by_token(token, db)
    if user is None:
        # Covers: unknown token, revoked, expired, and orphaned-user-row --
        # all indistinguishable to the caller, same as ARCHITECTURE.md
        # §3.2's sequence diagram.
        raise _unauthorized()

    return user
