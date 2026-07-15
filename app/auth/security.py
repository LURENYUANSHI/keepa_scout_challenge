"""Password hashing and token generation primitives.

See ARCHITECTURE.md §2 / §6:
- Passwords are hashed with bcrypt (via the `bcrypt` package directly, no
  passlib). bcrypt only looks at the first 72 bytes of its input — silently
  truncating a longer password would let a user believe they set a much
  longer password than what's actually being checked. We refuse that
  outright instead: anything over 72 bytes raises ValueError, and callers
  (app/routers/auth.py) are expected to validate length *before* calling
  hash_password so this is a defensive backstop, not the primary UX.
- Auth tokens: the raw token is only ever returned to the client once, at
  issuance. What we store in `auth_tokens.token_hash` is a SHA-256 hex
  digest of it — a DB leak doesn't hand out usable bearer tokens.
"""
import hashlib
import secrets

import bcrypt

# bcrypt's own hard limit on password bytes. See module docstring.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """Hash `password` with bcrypt. Raises ValueError if it's >72 bytes."""
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password is {len(pw_bytes)} bytes, which exceeds bcrypt's "
            f"{MAX_PASSWORD_BYTES}-byte input limit. Refusing to silently "
            "truncate it — use a shorter password."
        )
    hashed = bcrypt.hashpw(pw_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check `password` against a bcrypt hash produced by hash_password.

    A password over the 72-byte limit can never be one we hashed (we'd have
    rejected it at registration time), so it's simply treated as "no match"
    rather than raising — this is the login-time symmetric case of the
    reject-don't-truncate rule.
    """
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(pw_bytes, password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/foreign hash format — treat as no match, not a crash.
        return False


def generate_token() -> str:
    """A cryptographically random bearer token (URL-safe, ~256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a bearer token — what gets stored/looked up."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
