"""Multi-interval price bars and their audit tables.

A sibling of price_history (interval='1d'), not a copy: a price_history
row is keyed by the exchange's local session date, a price_bars row by
the absolute timestamp; a bar's local calendar day is not its session."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    BarIntervalType,
    Base,
    PriceType,
    TsType,
    symbol_fk_column,
)

# Single source for the intervals actually written. Excluded, with
# reasons: 2m (derivable from 1m), 30m (not returned by Yahoo; resample
# from 15m), 90m (non-standard), 1h (same as 60m), 5d (broken), 3mo
# (repair alignment drifts by month).
BAR_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m", "1wk", "1mo")

# Intraday intervals. is_extended is only meaningful for these, and the
# rescale UPDATE applies only to these: 1wk/1mo are re-fetched from scratch
# with period="max" on every run, so they already arrive at Yahoo's current
# scale and rescaling them would double-adjust after a partial fetch.
INTRADAY_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m")

# Intervals above daily live in `periodic_bars`, not the hypertable: their
# period="max" range spans decades, which in a 7-day-chunk hypertable
# explodes the chunk count for a tiny fraction of the rows.
PERIODIC_INTERVALS: tuple[str, ...] = ("1wk", "1mo")


#: The daily series predates the bar tables and has its own shape: it is
#: keyed on the exchange's session date, carries adj_close, and has no
#: bar_interval column. It is not in BAR_INTERVALS because nothing writes
#: it through the bar datasets -- but a reader still has to resolve "1d"
#: to a table, so the mapping belongs here rather than in a second copy.
DAILY_INTERVAL = "1d"

#: Every interval this project can resolve to storage.
READABLE_INTERVALS: tuple[str, ...] = (*INTRADAY_INTERVALS, DAILY_INTERVAL, *PERIODIC_INTERVALS)

#: The same set as a type, so an API parameter can be annotated with it and
#: the published contract lists the values instead of saying "string".
#: Spelled out because `Literal[*READABLE_INTERVALS]` runs but does not type
#: check -- a Literal's arguments have to be visible statically. A test
#: asserts the two stay equal, which is what makes the duplication safe.
ReadableInterval = Literal["1m", "5m", "15m", "60m", "1d", "1wk", "1mo"]


def bars_table_for(interval: str) -> str:
    """Resolves an interval to the table it is stored in.

    Dataset writes, watermark reads, the read API and tests all use this;
    a divergence would write to one table and read the watermark from
    another. Unknown intervals raise rather than default to a table."""
    if interval in INTRADAY_INTERVALS:
        return "price_bars"
    if interval in PERIODIC_INTERVALS:
        return "periodic_bars"
    if interval == DAILY_INTERVAL:
        return "price_history"
    raise ValueError(f"unknown interval: {interval!r}")

# bar_gaps.reason values
GAP_RETENTION_EXPIRED = "retention_expired"
GAP_FETCH_FAILED = "fetch_failed"

# bar_gaps.resolved_by values
RESOLVED_BY_TICKS = "ticks"


class PriceBar(Base):
    """Intraday bars (1m/5m/15m/60m). Sibling of price_history.

    Carries an FK to `symbols` (a hypertable can be the referencing side),
    at the cost of a shared lock on the symbols row per insert. 1wk/1mo go
    to `periodic_bars` so their multi-decade range does not inflate chunks."""

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # The bar's local calendar date. Not named session_date:
    # price_history.session_date is a session day, while an evening bar's
    # calendar day can differ from its session day.
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_price_bars_volume_nonneg")
    )

    # Outside the regular session (pre/post market). Derived from
    # tradingPeriods' start/end range, not has_pre_post_market_data, which
    # some symbols report False while still returning extended bars.
    is_extended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class IntradayScope(Base):
    """Symbol subset for 1m (and other intervals, if enabled).

    Data, not configuration: a subset of a large universe does not fit
    in .env and needs a versioned, mutable table."""

    __tablename__ = "intraday_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class PeriodicBar(Base):
    """Bars above daily (1wk/1mo). Not a hypertable.

    Separate from `price_bars`: decades of range at low density need no
    chunking, rescale never applies (always re-fetched with period="max"),
    and is_extended is meaningless above daily."""

    __tablename__ = "periodic_bars"
    __table_args__ = (
        Index("ix_periodic_bars_local_date", "local_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_periodic_bars_volume_nonneg")
    )


class BarGap(Base):
    """Permanent record of missed fetch windows.

    "No data" can mean market closed or fetch missed; once Yahoo's window
    has passed that cannot be reconstructed, so it is recorded at the time."""

    __tablename__ = "bar_gaps"
    __table_args__ = (Index("ix_bar_gaps_detected_at", "detected_at"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    gap_start_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    gap_end_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    reason: Mapped[str] = mapped_column(AsciiKeyType(24), nullable=False)
    # NULL = the gap is still open; the scheduler retries open gaps on every
    # run, since the watermark moves past a dropped slice otherwise.
    # retention_expired gaps can only be closed from the live tick archive
    # (`yfin stream reconcile`).
    resolved_at: Mapped[datetime | None] = mapped_column(TsType())

    # What closed the gap. A separate column rather than a `reason` value:
    # `reason` is inside the gap write's update_columns
    # (datasets/bars.py), so the next detection of the same key would
    # overwrite it and the provenance would be lost. This column is never
    # in that scope.
    resolved_by: Mapped[str | None] = mapped_column(AsciiKeyType(16))


class BarRescale(Base):
    """Ledger of applied retroactive rescalings.

    Idempotency gate: the UPDATE and this record write in one transaction.
    `yfin rescale --seed` must write a ratio=1 baseline for every existing
    split first, or a first run would reapply every historical split."""

    __tablename__ = "bar_rescales"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    split_date: Mapped[date] = mapped_column(primary_key=True)
    # DECIMAL, not float: a 3:2 split gives ratio 1.5, and float division
    # accumulates drift across millions of rows.
    ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    rows_affected: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint('"rows_affected" >= 0', name="ck_bar_rescales_rows_affected_nonneg"),
        nullable=False,
    )


def timescale_ddl() -> tuple[str, ...]:
    """Hypertable DDL for price_bars and price_history; shared by the
    migration and the test conftest since Alembic cannot autogenerate it.
    `create_default_indexes => FALSE`: the default DESC index is absent from
    Base.metadata and would block autogenerate's empty-diff gate. '365 days'
    not '1 year': TimescaleDB records month-bearing intervals as 30-day months."""
    return (
        "SELECT create_hypertable('price_bars', "
        "by_range('ts_utc', INTERVAL '7 days'), "
        "create_default_indexes => FALSE)",
        "SELECT create_hypertable('price_history', "
        "by_range('session_date', INTERVAL '365 days'), "
        "create_default_indexes => FALSE)",
    )
