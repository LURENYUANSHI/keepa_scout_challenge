"""asins, asin_price_stats — see ARCHITECTURE.md §2.

Indexes on computed_roi_pct / eligible / amazon_buybox_pct per
CHALLENGE.md's "index the filterable columns" requirement.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Asin(Base):
    __tablename__ = "asins"

    asin: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    buybox: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    referral_fee_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    sales_rank: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amazon_buybox_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    monthly_sold: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    filter_failed: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_roi_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    supplier_cost: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    price_stats: Mapped["AsinPriceStats | None"] = relationship(
        back_populates="asin_ref", uselist=False
    )

    __table_args__ = (
        Index("ix_asins_computed_roi_pct", "computed_roi_pct"),
        Index("ix_asins_eligible", "eligible"),
        Index("ix_asins_amazon_buybox_pct", "amazon_buybox_pct"),
    )


class AsinPriceStats(Base):
    __tablename__ = "asin_price_stats"

    asin: Mapped[str] = mapped_column(Text, ForeignKey("asins.asin"), primary_key=True)
    avg_90d: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    min_90d: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    current_deviation_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    asin_ref: Mapped["Asin"] = relationship(back_populates="price_stats")
