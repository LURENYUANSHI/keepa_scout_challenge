"""chat_sessions — ownership-only table, see ARCHITECTURE.md §2.

Deliberately does NOT model active_filters/last_result_asins/messages —
those live in LangGraph's own checkpointer/store tables (AsyncPostgresSaver
/ AsyncPostgresStore), set up separately in app/agent/ (Phase 4). This
table's only job is answering "does session_id X belong to user Y".
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Session-list UI additions (history/resume feature). Both are
    # application-managed, not DB-trigger-managed: app/routers/chat.py's
    # `ensure_session_ownership` sets `updated_at` on every turn (creation
    # AND every subsequent turn) and sets `title` exactly once, from the
    # first ~60 chars of the session's first user message, the moment it's
    # still NULL -- never overwritten after that. `onupdate=func.now()` is
    # only a defensive backstop for any future direct-ORM-update path that
    # forgets to set it explicitly, not the primary mechanism.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
