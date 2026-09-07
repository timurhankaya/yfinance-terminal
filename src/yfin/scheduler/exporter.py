"""A daemon thread that turns the database into gauges.

Prometheus scrapes the scheduler every fifteen seconds. If a scrape ran the
queries in `queries.py`, fifteen seconds would buy a full pass over
`sync_run_items` -- and a second Prometheus, a curl, or a dashboard's
"refresh now" would each buy another. So the queries run on their OWN clock,
every `yf_exporter_interval_seconds`, and a scrape reads the last values
they left behind. The endpoint never touches a connection.

Three consequences, all deliberate:

  * A gauge is up to one interval old. That is stated on the dashboards
    rather than hidden, and 300 seconds is a fraction of every cadence the
    numbers are about.
  * A query that fails keeps its previous values. The alert is
    `yfin_exporter_last_success_timestamp` going stale, not a gauge
    dropping to zero -- a zero would fire every alert that reads it at once
    and say nothing true.
  * A label combination only disappears when its query SUCCEEDS: each query
    clears the gauges it owns immediately before republishing them. That is
    what retires a dataset that left the universe or a proxy that was
    deleted, and what stops a failed query from wiping numbers another one
    just filled.

The thread lives in the scheduler process because that process already
exists, already has an engine, and is the one thing in the stack that is
running whether or not anything else is. It is a daemon thread: an exporter
that kept the scheduler alive at shutdown would be an exporter that has to
be killed.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import clear_gauge, count_exception, set_gauge
from yfin.scheduler.queries import QUERIES, Context, Query, Sample

log = get_logger(__name__)

#: What the scheduler passes so the exporter can publish the job gauges it
#: alone knows -- next fire time, whether a subprocess is alive right now.
#: A callable rather than the service itself: the exporter has no business
#: reaching into `SchedulerService`, and a test needs neither.
JobSamples = Callable[[], Iterable[Sample]]


def publish(samples: Iterable[Sample]) -> int:
    """Sets every sample as a gauge. Returns how many."""
    count = 0
    for sample in samples:
        set_gauge(sample.name, sample.value, **sample.labels)
        count += 1
    return count


class Exporter:
    """The refresh loop, and one pass of it.

    Constructed with an engine rather than a session factory so it owns its
    own sessions: the queries are read-only and long, and sharing the
    scheduler's would put a reporting scan in the same connection as the
    row that records a job start.
    """

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        intervals: Callable[[], dict[str, float]] | None = None,
        job_samples: JobSamples | None = None,
        queries: tuple[Query, ...] = QUERIES,
    ) -> None:
        self._factory: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )
        self._settings = settings
        # A callable, not a dict: the schedule is reloaded every sixty
        # seconds and a cadence captured at start-up would keep judging
        # freshness against a cron nobody runs any more.
        self._intervals = intervals or dict
        self._job_samples = job_samples
        self._queries = queries
        self._stopping = threading.Event()

    # --- one pass ----------------------------------------------------------

    def context(self) -> Context:
        return Context(
            session_factory=self._factory,
            settings=self._settings,
            intervals=self._intervals(),
            now=datetime.now(UTC),
        )

    def refresh(self) -> int:
        """One full pass. Returns the number of queries that failed.

        Every query is attempted even after one fails: they read different
        tables and a stream outage has nothing to say about whether the
        freshness numbers can be produced. The success timestamp only moves
        when the count is zero, which is what makes `ExporterStale` mean
        "some part of this is blind" rather than "all of it is".
        """
        ctx = self.context()
        failed = 0
        for query in self._queries:
            started = time.monotonic()
            try:
                samples = query.run(ctx)
            except Exception as exc:  # noqa: BLE001 - one blind query, not a dead exporter
                failed += 1
                count_exception(exc)
                log.warning(
                    "exporter query failed; keeping the previous values",
                    query=query.name,
                    error=str(exc),
                )
                continue
            for name in query.gauges:
                clear_gauge(name)
            publish(samples)
            set_gauge(
                "yfin_exporter_query_seconds", time.monotonic() - started, query=query.name
            )

        if self._job_samples is not None:
            # Not a `Query`: it reads the scheduler's memory, not the
            # database, so it cannot fail the way a query can and owns no
            # gauges anything else could clear.
            try:
                publish(self._job_samples())
            except Exception as exc:  # noqa: BLE001 - same rule as a query
                failed += 1
                count_exception(exc)
                log.warning("job gauges failed", error=str(exc))

        if failed == 0:
            set_gauge("yfin_exporter_last_success_timestamp", time.time())
        return failed

    # --- the thread --------------------------------------------------------

    def start(self) -> threading.Thread:
        """Starts the loop and returns its thread. Refreshes immediately.

        The first pass happens before the first sleep, so a scrape arriving
        one second after start-up sees numbers rather than an empty
        endpoint that reads as "everything is zero".
        """
        thread = threading.Thread(target=self._loop, name="yfin-exporter", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stopping.set()

    def _loop(self) -> None:
        interval = self._settings.yf_exporter_interval_seconds
        log.info("exporter started", interval_seconds=interval)
        while True:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one pass
                count_exception(exc)
                log.error("exporter pass failed", error=str(exc))
            if self._stopping.wait(interval):
                return


__all__ = ["Exporter", "JobSamples", "publish"]
