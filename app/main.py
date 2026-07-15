"""FastAPI entry point — mounts all routers (see ARCHITECTURE.md §1 `api` service).

Phase 1 (this file, as scaffolded): app boots, DB tables get created, and a
public /health check exists. Phase 3a adds /upc and /eligibility. Phase 4
(this revision) adds the LangGraph checkpointer/store lifecycle and the
/ask, /chat routers.
"""
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.checkpointer import checkpointer_lifespan
from app.agent.graph import build_graph
from app.agent.store import store_lifespan
from app.config import settings
from app.db import init_db
from app.routers import ask, auth, chat, eligibility, refresh, upc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()

    # LangGraph's checkpointer (short-term memory, per thread_id) and store
    # (long-term memory, per user_id) each own a live Postgres connection
    # for the process lifetime -- both are async context managers, entered
    # once here via AsyncExitStack and cleanly closed on shutdown. See
    # ARCHITECTURE.md §2/§4 and app/agent/checkpointer.py / store.py.
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(
            checkpointer_lifespan(settings.DATABASE_URL)
        )
        store = await stack.enter_async_context(store_lifespan(settings.DATABASE_URL))

        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.agent_graph = build_graph(checkpointer, store)

        yield
        # AsyncExitStack closes the checkpointer/store connections here on
        # shutdown (reverse order of entry).


app = FastAPI(title="Keepa Scout", lifespan=lifespan)

# CORS — the Vue SPA (frontend/) is served from its own origin (Vite dev
# server on :5173, or the nginx container on :5173 in docker-compose), which
# is cross-origin from the API on :8000. Wide open (`allow_origins=["*"]`
# equivalent via regex) rather than a hardcoded allowlist because the exact
# dev-server port varies by how someone runs this (npm run dev vs docker
# compose vs a preview build) and there's no cookie-based auth here to make
# a permissive CORS policy a credential-leak risk (auth is a bearer token
# the client attaches explicitly, not an ambient cookie).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness check — see HARNESS.md §0."""
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(upc.router)
app.include_router(eligibility.router)
app.include_router(refresh.router)
app.include_router(ask.router)
app.include_router(chat.router)
