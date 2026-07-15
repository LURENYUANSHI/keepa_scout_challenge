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
