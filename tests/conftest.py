"""Shared pytest fixtures.

Points at a Postgres test database (defaults to a `_test` suffixed variant
of DATABASE_URL if TEST_DATABASE_URL isn't set explicitly). Creates all
tables once per test session, then wraps each test in a transaction that's
rolled back afterwards so tests don't leak state into each other.

Later phases (routers, tools, agent) build on top of the `db_session` /
`app` fixtures here rather than each writing their own DB bootstrap.
"""
import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base, get_db


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://keepa_scout:keepa_scout@localhost:5432/keepa_scout",
    )
    # Swap the trailing db name for a dedicated test DB, e.g.
    # .../keepa_scout -> .../keepa_scout_test
    root, _, db_name = base_url.rpartition("/")
    return f"{root}/{db_name}_test" if db_name else base_url


TEST_DATABASE_URL = _test_database_url()


# NOTE (fixed in Phase 2a): this file used to define its own session-scoped
# `event_loop` fixture to keep the session-scoped `test_engine` fixture on a
# single loop. That override pattern is deprecated in pytest-asyncio 0.23+
# and — worse — wasn't actually honored by test functions running under
# `asyncio_mode = auto` (they got their own per-function loop from
# pytest-asyncio instead), so the asyncpg pool underneath `test_engine` ended
# up straddling multiple event loops and raised "cannot perform operation:
# another operation is in progress" on the second query. The fix is
# pytest.ini's `asyncio_default_fixture_loop_scope` /
# `asyncio_default_test_loop_scope = session`, which puts fixtures *and*
# tests on the same session-scoped loop without a custom fixture.


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    import app.models  # noqa: F401  registers models on Base.metadata

    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncSession:
    """A session bound to a per-test transaction, rolled back on teardown."""
    connection = await test_engine.connect()
    transaction = await connection.begin()

    session_maker = async_sessionmaker(bind=connection, expire_on_commit=False)
    session = session_maker()

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def client(test_engine):
    """An httpx AsyncClient wired directly to the FastAPI app (no real socket).

    NOTE (fixed in Phase 2a): as originally scaffolded this fixture wired up
    the app without touching `app.db.get_db` at all, so every request would
    have gone through app/db.py's *production* engine (DATABASE_URL) instead
    of the test database this file otherwise goes to such lengths to set up.
    On top of pointing at the wrong DB, that production engine targets the
    `db` compose hostname, which isn't even resolvable when pytest runs
    outside the compose network. We override `get_db` here to hand out
    sessions bound to `test_engine` (TEST_DATABASE_URL) instead, and reset
    the auth tables before each test so tests don't leak state into each
    other via unique-email collisions.
    """
    from app.main import app

    test_session_maker = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db

    async with test_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE TABLE auth_tokens, users RESTART IDENTITY CASCADE")
        )

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)
