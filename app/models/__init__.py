"""ORM models — one module per table group, mirroring ARCHITECTURE.md §2's ER diagram.

Importing this package registers every model on `app.db.Base.metadata`, which
is what `init_db()` needs to see before calling `Base.metadata.create_all`.
"""
from app.models.asin import Asin, AsinPriceStats
from app.models.chat import ChatSession
from app.models.refresh import RefreshJob, RefreshJobItem
from app.models.usage import LlmUsageLog
from app.models.user import AuthToken, User

__all__ = [
    "Asin",
    "AsinPriceStats",
    "ChatSession",
    "RefreshJob",
    "RefreshJobItem",
    "LlmUsageLog",
    "AuthToken",
    "User",
]
