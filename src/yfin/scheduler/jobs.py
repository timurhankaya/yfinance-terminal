"""What the scheduler can run, and which queue each job waits in.

The set of jobs is fixed here; only their timing is configurable. Every
job is a subprocess: the scheduler replaces cron, not the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

#: The two queues. `yahoo` is single-threaded, so the four jobs that talk to
#: Yahoo or take the sync advisory lock can never run at the same time and
#: split one IP's rate budget between them. The rest have no such
#: relationship and share the default pool.
Executor = Literal["yahoo", "default"]

#: Written into the subprocess's environment; `audit.open_run` reads it and
#: stores it on `sync_runs.job_run_id`, which is what joins a scheduler run
#: to the sync it produced without changing any command signature.
JOB_RUN_ID_VAR = "YF_JOB_RUN_ID"


@dataclass(frozen=True)
class Job:
    """One schedulable command."""

    #: Also the value of the `job` label and of `scheduler_runs.job`.
    name: str
    #: The setting holding its cron expression. Empty means not registered.
    setting: str
    #: Argv after `yfin`.
    command: tuple[str, ...]
    executor: Executor
    description: str


JOBS: tuple[Job, ...] = (
    Job(
        name="sync",
        setting="yf_schedule_sync",
        command=("sync",),
        executor="yahoo",
        description="Every active symbol, every dataset.",
    ),
    Job(
        name="market",
        setting="yf_schedule_market",
        command=("market", "sync"),
        executor="yahoo",
        description="Market-wide calendars, status and summary.",
    ),
    Job(
        name="domain",
        setting="yf_schedule_domain",
        command=("domain", "sync"),
        executor="yahoo",
        description="Sector and industry taxonomy.",
    ),
    Job(
        name="stream_reconcile",
        setting="yf_schedule_stream_reconcile",
        command=("stream", "reconcile"),
        executor="yahoo",
        # Not because it fetches -- it does not -- but because it takes the
        # SYNC advisory lock: it writes price_bars.
        description="Fill open 1m gaps from the tick archive.",
    ),
    Job(
        name="bars_maintain",
        setting="yf_schedule_bars_maintain",
        command=("bars", "maintain"),
        executor="default",
        description="Report on the intraday archive. Writes nothing.",
    ),
    Job(
        name="prune",
        setting="yf_schedule_prune",
        command=("prune",),
        executor="default",
        description="Retention. Off by default: deletions are irreversible.",
    ),
    Job(
        name="usage_flush",
        setting="yf_schedule_usage_flush",
        command=("api", "usage", "flush"),
        executor="default",
        description="Move yesterday's API usage counters out of Redis.",
    ),
)

JOBS_BY_NAME: dict[str, Job] = {job.name: job for job in JOBS}


#: Long enough to cover a monthly job several times over, so the mean below
#: is not distorted by where in the month the sample starts.
_INTERVAL_SAMPLE_DAYS = 400


def interval_seconds(trigger: Any, *, now: datetime | None = None) -> float:
    """The cron's MEAN period in seconds: first-to-last firing span over the gap count.

    Dividing the window by the firing count instead would swing with where
    the window starts. 0.0 when fewer than two firings fall in the window.
    """
    start = now or datetime.now(trigger.timezone)
    end = start + timedelta(days=_INTERVAL_SAMPLE_DAYS)

    first: datetime | None = None
    last: datetime | None = None
    gaps = 0
    previous = start
    while True:
        nxt = trigger.get_next_fire_time(previous, previous)
        if nxt is None or nxt > end:
            break
        if first is None:
            first = nxt
        else:
            gaps += 1
        last = nxt
        previous = nxt

    if first is None or last is None or gaps == 0:
        return 0.0
    return (last - first).total_seconds() / gaps


__all__ = [
    "JOBS",
    "JOBS_BY_NAME",
    "JOB_RUN_ID_VAR",
    "Executor",
    "Job",
    "interval_seconds",
]
