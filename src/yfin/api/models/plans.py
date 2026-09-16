"""Plans and measured usage. Plan limits live in a table, read through a
short-lived process cache, so changing them needs neither a deploy nor a
per-request query."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.core.families import EXTRA_USAGE_FAMILIES, DataFamily
from yfin.models.base import Base, TsType

PLAN_NAME_LENGTH = 16


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """Writes enum VALUES and names the PostgreSQL type explicitly; an unnamed
    Enum becomes a permanent CREATE TYPE with the Python class's casing."""
    return Enum(cls, values_callable=lambda e: [m.value for m in e], name=name)


#: Families plus the two surfaces that belong to none. Derived, not
#: retyped: a new DataFamily shows up here without a second edit.
UsageFamily = enum.StrEnum(  # type: ignore[misc]
    "UsageFamily",
    {f.name: f.value for f in DataFamily} | {n.upper(): n for n in EXTRA_USAGE_FAMILIES},
)


class ApiPlan(Base):
    """One row per plan. Seeded by the migration, edited by an operator."""

    __tablename__ = "api_plans"
    __table_args__ = (
        CheckConstraint('"requests_per_second" > 0', name="ck_api_plans_rps_positive"),
        CheckConstraint('"burst" > 0', name="ck_api_plans_burst_positive"),
        CheckConstraint('"monthly_quota" > 0', name="ck_api_plans_quota_positive"),
        CheckConstraint('"max_page_size" > 0', name="ck_api_plans_page_size_positive"),
        CheckConstraint('"max_concurrency" > 0', name="ck_api_plans_concurrency_positive"),
        # A burst below the steady rate would mean the bucket cannot even
        # hold one second's worth of tokens: the configured rate would be
        # unreachable and the limiter would reject traffic the plan sells.
        CheckConstraint('"burst" >= "requests_per_second"', name="ck_api_plans_burst_covers_rate"),
    )

    plan: Mapped[str] = mapped_column(String(PLAN_NAME_LENGTH, collation="C"), primary_key=True)
    requests_per_second: Mapped[int] = mapped_column(Integer, nullable=False)
    burst: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_quota: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_page_size: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )


class ApiUsageDaily(Base):
    """Request counts per client, day and family. `estimated` marks a row the
    counters could not measure exactly (Redis unreachable, limiter failed
    open), so billing can tell a measured row from a reconstructed one."""

    __tablename__ = "api_usage_daily"

    client_id: Mapped[str] = mapped_column(
        ForeignKey("api_clients.client_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    endpoint_family: Mapped[str] = mapped_column(
        _enum(UsageFamily, "api_usage_family"), primary_key=True
    )
    request_count: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint('"request_count" >= 0', name="ck_api_usage_daily_count_nonneg"),
        nullable=False,
        server_default=text("0"),
    )
    estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
