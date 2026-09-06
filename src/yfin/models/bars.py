"""Multi-interval price bars and their audit tables.

A sibling of price_history (interval='1d'), not a copy: key semantics
differ. A price_history row's authority is the exchange's local session
date; a price_bars row's is the absolute timestamp. GC=F shows the
difference -- its bar opens at 18:10, so the bar's local calendar day is
not its session day.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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

# Intraday intervals. Kept separate because it changes semantics in two
# places:
#   1. is_extended is only meaningful for these.
#   2. Rescale UPDATE applies only to these: 1wk and 1mo are always
#      re-fetched from scratch with period="max" on every run, so they
#      always arrive at Yahoo's current scale. Rescaling them too would
#      leave rows double-adjusted if a run's fetch failed partway, and
#      bar_rescales would consider the split "applied" and never fix it.
INTRADAY_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m")

# Intervals above daily. Written to a separate table (`periodic_bars`),
# and this is a measured necessity, not a storage optimization:
#
# 1wk/1mo are 0.07% of rows (~320k rows/year across 5,000 symbols) but
# 100% of the time range: `period="max"` reaches back to 1980. In the
# same hypertable with a 7-day chunk interval that produces
# 16,700 / 7 = ~2,386 chunks -- measured: 2,388 chunks for 21,934 rows in
# one symbol, ~9 rows per chunk. Seven-thousandths of the data caused the
# entire chunk explosion.
#
# Splitting them out shrinks `price_bars`'s range to 60m's 729 days
# (~104 chunks), and `periodic_bars` is not a hypertable: a 46-year
# backfill is ~15 million rows, no chunking needed.
#
# MySQL handled this with a single historical partition named `p_hist`;
# TimescaleDB has no equivalent, so the split has to be explicit here.
PERIODIC_INTERVALS: tuple[str, ...] = ("1wk", "1mo")


def bars_table_for(interval: str) -> str:
    """Resolves an interval to the table it is written to.

    Single source of truth: dataset writes, watermark reads, and tests
    all use this. If they diverged, an interval would write to the wrong
    table and its watermark would stay NULL forever -- a full backfill
    on every run.
    """
    return "price_bars" if interval in INTRADAY_INTERVALS else "periodic_bars"

# bar_gaps.reason values
GAP_RETENTION_EXPIRED = "retention_expired"
GAP_FETCH_FAILED = "fetch_failed"


class PriceBar(Base):
    """Intraday bars (1m/5m/15m/60m). Sibling of price_history.

    Carries an FK. MySQL 8's partitioned InnoDB table did not support
    foreign keys (ERROR 1506); integrity was enforced by the write path
    plus a monthly orphan-row query. A TimescaleDB hypertable can be the
    referencing side (measured: ON UPDATE CASCADE + ON DELETE RESTRICT
    work, and drop_chunks is unaffected by an outgoing FK), so integrity
    is now DB-level and the orphan-row query is unnecessary.

    Accepted cost: every insert takes a shared lock on the `symbols` row,
    and price_bars is the most heavily written table.

    Intraday only: 1wk/1mo go to `periodic_bars` (see PERIODIC_INTERVALS).
    In the same table their 46-year range would inflate chunk count twentyfold.
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # Derived column: the bar's local calendar date. Deliberately not
    # named session_date -- price_history.session_date is a session day,
    # this is only a local calendar day. Sharing the name would conflate
    # two different concepts (GC=F: an 18:10 bar whose session is the
    # next day).
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_price_bars_volume_nonneg")
    )

    # Whether the bar is outside the regular session (pre/post market).
    # Single source of truth is tradingPeriods' start/end range; not
    # has_pre_post_market_data -- SHEL.L and VWCE.DE report it False while
    # still returning extended-session bars.
    is_extended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class IntradayScope(Base):
    """Symbol subset for 1m (and other intervals, if enabled).

    This list is data, not configuration: in a 5,000-symbol universe, a
    500-symbol subset does not fit in .env and needs a versioned, mutable
    table.
    """

    __tablename__ = "intraday_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class PeriodicBar(Base):
    """Bars above daily (1wk/1mo). Not a hypertable.

    Kept separate from `price_bars` because the two tables differ on
    every operational dimension:
      * Time range: 46 years here vs. 729 days there.
      * Density: ~320k rows/year across 5,000 symbols here vs. ~464 million there.
      * Rescale: retroactive rescaling applies only to intraday -- this
        table is always re-fetched from scratch with period="max", so it
        always arrives at Yahoo's current scale.
      * is_extended: the pre/post-market concept is meaningless above
        daily, so there is no such column.

    Not chunked: a 46-year backfill is ~15 million rows, and the PK index
    is enough. Making this a hypertable would bring back the chunk
    explosion this split fixed.
    """

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

    "No data" in the archive can mean two different things: the market
    was closed, or the fetch was missed. That distinction cannot be
    reconstructed later -- once Yahoo's window has passed, "was there a
    bar here" has no answer. If it is not recorded at the time, the
    information is gone for good.
    """

    __tablename__ = "bar_gaps"
    __table_args__ = (Index("ix_bar_gaps_detected_at", "detected_at"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    gap_start_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    gap_end_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    reason: Mapped[str] = mapped_column(AsciiKeyType(24), nullable=False)
    # NULL = the gap is still open. The scheduler retries open gaps on
    # every run; without this feedback bar_gaps would be a mere
    # tombstone: if a middle slice is dropped but later ones are written,
    # the watermark moves past the gap and that window is never
    # requested again. retention_expired rows always stay NULL here.
    resolved_at: Mapped[datetime | None] = mapped_column(TsType())


class BarRescale(Base):
    """Ledger of applied retroactive rescalings.

    The idempotency gate: without this table, the same split would be
    reapplied on a second run and corrupt the archive again. The UPDATE
    and this record write in the same transaction.

    A seed step is mandatory: the splits table is already populated by
    the existing pipeline. A first run against an empty bar_rescales
    would apply every historical split, e.g. dividing AAPL's archive by
    2*2*2*7*4 = 224 -- even though those bars already arrive from Yahoo
    at the current scale. `yfin rescale --seed` writes a ratio=1 baseline
    record for every existing split.
    """

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
    """Hypertable DDL for price_bars and price_history.

    Alembic cannot autogenerate this; migration and the test conftest
    both use this same constant (the pattern the project already uses
    for V_ACTIONS_CREATE). Otherwise tests would run against plain
    tables with no hypertable, and chunk behavior would never be verified.

    `create_default_indexes => FALSE` is required. The default behavior
    creates a DESC index named `price_bars_ts_utc_idx` on the
    partitioning column; that index lives in the `public` schema, is
    absent from Base.metadata, and Alembic autogenerate reports it as
    "should be dropped" -- so `yfin db revision`'s "empty diff" gate never
    opens. Needed indexes are defined explicitly in the model
    (ix_price_bars_local_date, ix_price_history_session_date); ts_utc
    needs no separate index since it is the last PK component.

    `INTERVAL '1 year'` is not used: TimescaleDB converts a month-bearing
    interval to 30-day months, recording the range as 360 days (measured).

    `periodic_bars` is deliberately absent from this list: 1wk/1mo span
    46 years but are only ~15 million rows; making it a hypertable would
    bring back the chunk explosion (see PERIODIC_INTERVALS).

    Monthly partitions need no manual creation: chunks are created at
    write time. There is no "insert outside range" concept, so MySQL's
    dilemma of "loud ERROR 1526 vs. silent pruning death" disappears
    entirely -- the single biggest win of this migration.
    """
    return (
        "SELECT create_hypertable('price_bars', "
        "by_range('ts_utc', INTERVAL '7 days'), "
        "create_default_indexes => FALSE)",
        "SELECT create_hypertable('price_history', "
        "by_range('session_date', INTERVAL '365 days'), "
        "create_default_indexes => FALSE)",
    )
