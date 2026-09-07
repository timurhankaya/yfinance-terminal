"""What the pipeline records about running itself.

Two tables, one per kind of process, and both exist because the process that
knows the number is not the process anyone can ask.

`scheduler_runs` is the scheduler's own log: a row per firing, opened before
the subprocess starts and closed when it exits. Without it "did last night's
sync run at all" is answerable only from the sync's own audit -- which says
nothing about a job that never started, misfired, or was killed.

`run_metrics` is where a sync shard leaves its counters. A shard is a
short-lived process; it exits long before any scrape could reach it, so
`core/metrics.py` accumulates in memory and flushes here on the way out, and
the exporter turns the table into gauges. Written OUTSIDE the symbol
transactions, so a metrics failure can never roll back data.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import AsciiKeyType, Base, TsType

#: `result` values the scheduler writes itself, rather than deriving from an
#: exit code. `misfired` means the firing was dropped before it ran,
#: `skipped` that a previous instance was still going, `terminated` that
#: SIGTERM ran out of patience -- none of which produce an exit code.
SCHEDULER_RESULTS = ("ok", "partial", "locked", "failed", "misfired", "skipped", "terminated")


class SchedulerRun(Base):
    """One firing of one scheduled job.

    A row is INSERTED before the subprocess starts, so a process that dies
    between fork and exit still leaves a record. On start-up the scheduler
    closes any row with `finished_at IS NULL` as `terminated`; if the
    subprocess is somehow still alive, the advisory lock makes the next run
    `locked`, which is visible rather than a silent overlap.
    """

    __tablename__ = "scheduler_runs"
    # The Freshness dashboard asks "when did this job last succeed" and
    # "how late was it", both of which walk one job backwards in time.
    __table_args__ = (
        Index("ix_scheduler_runs_job_scheduled", "job", sa.desc("scheduled_at")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    job: Mapped[str] = mapped_column(AsciiKeyType(32), nullable=False)

    # When the trigger said it should run, NOT when it did. The difference
    # is the lateness a queued job accumulates behind the single-threaded
    # `yahoo` executor, and it is the number the dashboard shows.
    scheduled_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TsType())
    finished_at: Mapped[datetime | None] = mapped_column(TsType())

    # The subprocess's exit code, and the word derived from it. Both are
    # kept: the mapping (0 ok, 2 partial, 4 locked, everything else failed)
    # can be revisited later, and a stored code lets that happen without
    # the history becoming a lie.
    exit_code: Mapped[int | None] = mapped_column(SmallInteger)
    result: Mapped[str | None] = mapped_column(AsciiKeyType(16))

    # Kept so an operator can find the process, and so a restart can tell a
    # row it opened from one another scheduler did.
    pid: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class RunMetric(Base):
    """One counter, from one shard, for one run.

    Per SHARD, not per run. Shards are separate processes started with
    `spawn`, so the parent cannot see a child's memory; each flushes its own
    rows and the exporter sums them. `finalize_run` only closes the run.

    The key is (run_id, shard_index, name, labels) because that is what
    identifies a counter: the same metric appears once per label
    combination, and `labels` is canonical JSON so two increments written by
    different code paths land on one row rather than two
    (`core/metrics.label_key`).
    """

    __tablename__ = "run_metrics"

    # CASCADE: `--audit-days` prunes `sync_runs`, and a counter for a run
    # nobody kept is a number with nothing to attach it to.
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    shard_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str] = mapped_column(AsciiKeyType(48), primary_key=True)

    # Canonical JSON with sorted keys, `{}` when the counter has none. Part
    # of the key, which is why it must not be NULL and must not vary with
    # the order the labels were written in.
    labels: Mapped[str] = mapped_column(String(255, collation="C"), primary_key=True)

    value: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["SCHEDULER_RESULTS", "RunMetric", "SchedulerRun"]
