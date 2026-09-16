"""A daemon thread that turns the database into gauges.

Queries run on their own clock, never per scrape. A failed query keeps its
previous values; a query clears the gauges it owns only right before republishing.
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

    Owns its own sessions: a long reporting scan must not share the
    connection that records a job start.
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

        Every query is attempted even after one fails; the success
        timestamp only moves when none did.
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
        """Starts the loop and returns its thread.

        The first pass runs before the first sleep, so an early scrape
        does not read an empty endpoint as "everything is zero".
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
