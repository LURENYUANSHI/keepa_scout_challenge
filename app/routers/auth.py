"""POST /auth/register, POST /auth/login — ARCHITECTURE.md §3.1 / §6.

Also has a throwaway GET /auth/_whoami route: Phase 2a doesn't build any
other protected endpoint yet, so this is the only way to prove
`get_current_user` (§3.2) works end-to-end until later phases add real
protected routes that depend on it.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import (
    MAX_PASSWORD_BYTES,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.config import settings
from app.db import get_db
from app.models.user import AuthToken, User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

# Password policy — ARCHITECTURE.md §6: minimum 8 chars, no composition
# rules; upper bound is bcrypt's own 72-byte input limit, imported from
# app/auth/security.py (single source of truth) rather than redefined here
# — see that module's docstring for why we reject rather than silently
# truncate.
MIN_PASSWORD_BYTES = 8


def _validate_password_length(password: str) -> None:
    n = len(password.encode("utf-8"))
    if n < MIN_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password too short: got {n} bytes, need at least "
                f"{MIN_PASSWORD_BYTES}."
            ),
        )
    if n > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password too long: got {n} bytes, bcrypt only supports up "
                f"to {MAX_PASSWORD_BYTES} bytes. Use a shorter password "
                "instead of relying on truncation."
            ),
        )


def _invalid_credentials() -> HTTPException:
    # Same status + message for "no such user" and "wrong password" — don't
    # leak which emails are registered. ARCHITECTURE.md §3.1 / HARNESS.md §1.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
    )


async def _issue_token(db: AsyncSession, user_id: UUID) -> TokenResponse:
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.TOKEN_TTL_HOURS
    )
    db.add(
        AuthToken(
            token_hash=hash_token(token),
            user_id=user_id,
            expires_at=expires_at,
        )
    )
    await db.commit()
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    body: RegisterRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    _validate_password_length(body.password)

    email = body.email.strip()
    email_lower = email.lower()

    existing = await db.execute(
        select(User.id).where(func.lower(User.email) == email_lower)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered."
        )

    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # Race: someone else registered the same email between our SELECT
        # and this INSERT. The DB's unique index on lower(email) is the real
        # guard; the SELECT above is just the common-case fast path.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered."
        )

    # register auto-logs-in — ARCHITECTURE.md §6.
    return await _issue_token(db, user.id)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    email_lower = body.email.strip().lower()
    result = await db.execute(select(User).where(func.lower(User.email) == email_lower))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        raise _invalid_credentials()

    return await _issue_token(db, user.id)


@router.get("/_whoami")
async def whoami(user: User = Depends(get_current_user)) -> dict[str, str]:
    """Throwaway smoke test proving get_current_user works end-to-end.

    Nothing else should depend on this route — it exists solely so
    tests/test_auth.py (and manual curl verification) can exercise the auth
    dependency before any real protected endpoint exists.
    """
    return {"user_id": str(user.id)}
