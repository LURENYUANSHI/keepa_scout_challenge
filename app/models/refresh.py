"""refresh_jobs, refresh_job_items — see ARCHITECTURE.md §2 / §3.4.

refresh_job_items uses a composite PK on (job_id, asin): one row per
(job, asin) pair, which is exactly the granularity needed to resume a
partially-completed refresh (see ARCHITECTURE.md §3.4 "断点续跑").
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RefreshJob(Base):
    __tablename__ = "refresh_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)  # running/done
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)  # manual/scheduled
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RefreshJobItem(Base):
    __tablename__ = "refresh_job_items"

    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("refresh_jobs.job_id"), primary_key=True
    )
    asin: Mapped[str] = mapped_column(Text, ForeignKey("asins.asin"), primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)  # pending/done/failed
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
