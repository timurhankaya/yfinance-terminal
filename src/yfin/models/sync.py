"""sync_runs and sync_run_items audit tables."""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    ProxyLabelType,
    RegionType,
    SymbolType,
    TsType,
)


class RunScope(enum.StrEnum):
    """A run's scope: a symbol loop or a market loop."""

    SYMBOLS = "symbols"
    MARKET = "market"
    # Sector / industry type. A third axis, neither a symbol nor a region
    # loop -- 156 keys, each its own HTTP request.
    DOMAIN = "domain"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ItemStatus(enum.StrEnum):
    OK = "ok"
    EMPTY = "empty"  # No source data -- not an error.
    SKIPPED = "skipped"  # content_hash unchanged.
    FAILED = "failed"
    UNKNOWN_SYMBOL = "unknown_symbol"
    # Left unprocessed in the queue (shard pulled or died). Distinct from
    # SKIPPED, which means "content_hash unchanged" -- without this
    # distinction an unpulled symbol would look like "data is current".
    NOT_ATTEMPTED = "not_attempted"
    # Dataset deliberately not run for this symbol (outside
    # intraday_scope). Distinct from NOT_ATTEMPTED, whose meaning is
    # "shard was pulled, these symbols were not processed" -- a real gap
    # that makes RunTally.exit_code mark the run PARTIAL. Out-of-scope is
    # an intentional decision; writing not_attempted for 4,500 symbols
    # would make `yfin sync` return exit 2 every day.
    OUT_OF_SCOPE = "out_of_scope"


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # server_default backfills existing rows; the NOT NULL constraint does
    # not break on first run (ALGORITHM=INSTANT, 16ms measured on 50k rows).
    scope: Mapped[RunScope] = mapped_column(
        Enum(RunScope, values_callable=lambda e: [m.value for m in e], name="run_scope"),
        nullable=False,
        server_default=RunScope.SYMBOLS.value,
    )
    finished_at: Mapped[datetime | None] = mapped_column(TsType())
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus, values_callable=lambda e: [m.value for m in e], name="run_status"
        ),
        nullable=False,
    )
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dataset_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Written after proxy selection is known.
    shard_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    # This run's symbol universe and date range, human-readable
    # ("exchange=IST quote_type=EQUITY start=2020-01-01"). `scope` only
    # carries the symbols/market split; without this, which universe a
    # past run covered would be unknowable, and completeness claims
    # unauditable. No server_default: existing rows stay NULL = unfiltered.
    selector: Mapped[str | None] = mapped_column(String(255, collation="C"))
    rows_fetched: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_verified: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_skipped: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    # `scheduler_runs.id`, when a scheduled job produced this run. Read from
    # `YF_JOB_RUN_ID` by `audit.open_run`, so a scheduler run and the sync
    # it started are joinable without touching any command signature.
    #
    # No FK on purpose: the scheduler and the sync are separate processes
    # and a sync must not fail because the scheduler's row was pruned first.
    # It is also NULL for every manual run, which is the other half of the
    # `kind` split the exporter reports on.
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)


class SyncRunItem(Base):
    """For datasets writing to multiple tables, one row is written per table."""

    __tablename__ = "sync_run_items"
    __table_args__ = (
        Index("ix_sync_run_items_run_status", "run_id", "status"),
        Index("ix_sync_run_items_symbol_dataset", "symbol", "dataset"),
        Index("ix_sync_run_items_proxy", "proxy_id", "status"),
        # The freshness query walks one CELL -- (symbol, region, dataset) --
        # backwards to its latest run. Without this it is a full scan of a
        # table that grows by symbols x datasets every night, on a query the
        # exporter runs every five minutes.
        Index(
            "ix_sync_run_items_cell_run",
            "symbol",
            "region",
            "dataset",
            sa.desc("run_id"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    # No FK: an unresolvable symbol could not get an unknown_symbol record
    # written (FK violation). The audit record must survive symbol deletion.
    symbol: Mapped[str] = mapped_column(SymbolType(), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(
            ItemStatus, values_callable=lambda e: [m.value for m in e], name="item_status"
        ),
        nullable=False,
    )
    table_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    # The `ErrorKind` behind that message, when one was classified. The text
    # is for a human; this is what a dashboard can group by, and grouping by
    # free text would give one bucket per Yahoo error string.
    #
    # NULL where no kind is known -- a `not_attempted` row, or a failure
    # that never reached `classify_error`.
    error_kind: Mapped[str | None] = mapped_column(AsciiKeyType(16))
    # Region axis for domain cells; NULL for symbol and market runs.
    # `symbol` holds the domain SYMBOL (`^YH31130020`), not the key: the
    # column is VARCHAR(32) and five industry keys exceed that (longest 37).
    region: Mapped[str | None] = mapped_column(RegionType())

    shard_index: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    # No FK -- same reasoning as `symbol`, plus one more: each INSERT
    # takes a shared lock on the parent proxies row. A shard writing
    # thousands of items would S-lock its proxy row and wait on a
    # neighboring shard's health flush (X-lock). An FK would create
    # exactly the deadlock this avoids.
    proxy_id: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"proxy_id" >= 0', name="ck_sync_run_items_proxy_id_nonneg")
    )
    # Point-in-time copy: readable even if the proxy is later deleted.
    proxy_label: Mapped[str | None] = mapped_column(ProxyLabelType())
