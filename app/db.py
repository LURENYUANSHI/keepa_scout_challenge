"""Async SQLAlchemy engine/session setup.

Deliberately no migrations tool (no Alembic) — see ARCHITECTURE.md §5.
Schema is created via `Base.metadata.create_all` in `init_db()`, run once
at API startup (see app/main.py's lifespan). This is a take-home-scale
decision: a single `create_all` call is enough for a project with no
schema history to manage, and it keeps the stack one dependency lighter.
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession."""
    async with async_session_maker() as session:
        yield session


async def init_db() -> None:
    """Create all tables that don't exist yet (no-op if they already do).

    Importing app.models here (rather than at module top-level) avoids a
    circular import, since the models import `Base` from this module.
    """
    import app.models  # noqa: F401  (registers models on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
