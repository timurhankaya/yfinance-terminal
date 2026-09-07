"""The process that replaces cron.

One long-lived process holding an APScheduler `BlockingScheduler`. Each
firing spawns `yfin <command>` in ITS OWN PROCESS GROUP and waits for it.
The subprocess boundary is what keeps the scheduler simple: sharding,
advisory locks, per-proxy cache directories and exit codes are all the
runner's business, and a job that segfaults takes nothing with it.

Two executors, and the split is not tuning. `yahoo` is a single thread, so
`sync`, `market`, `domain` and `stream_reconcile` -- everything that talks
to Yahoo or takes the sync advisory lock -- can never run at the same time
and halve one IP's rate budget. Verified against APScheduler 3.11.3:
`max_instances` is counted PER JOB ID, so a DIFFERENT job fired while
`sync` runs is QUEUED rather than rejected, and its misfire grace is
evaluated when it is dequeued. So an hourly `stream_reconcile` waiting
behind a three-hour `sync` either runs late -- and the lateness is
`started_at - scheduled_at` in `scheduler_runs` -- or is dropped as
`misfired`. A second firing of a job that is itself still running is
`skipped`.

Settings are re-read on their own clock rather than through
`get_settings()`, which by design never re-reads: a scheduler that needed a
restart to pick up a new cron would defeat the reason cron expressions are
settings at all.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import tracing
from yfin.core.config import Settings, get_settings
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc
from yfin.scheduler import runs
from yfin.scheduler.exporter import Exporter
from yfin.scheduler.jobs import JOB_RUN_ID_VAR, JOBS, Job, interval_seconds
from yfin.scheduler.queries import Sample

log = get_logger(__name__)

#: How often the scheduler re-reads its own settings.
RELOAD_SECONDS = 60

#: The console script, next to the interpreter running the scheduler. Using
#: `sys.executable`'s sibling rather than PATH means a scheduler in a
#: virtualenv starts jobs from the SAME virtualenv, which is the one thing
#: that must not drift between the two.
YFIN = str(Path(sys.executable).with_name("yfin"))


@dataclass
class JobState:
    """What the scheduler knows about one job right now."""

    job: Job
    cron: str
    interval_seconds: float = 0.0
    running: bool = False
    last_duration_seconds: float | None = None
    last_success: datetime | None = None
    results: dict[str, int] = field(default_factory=dict)


class SchedulerService:
    """Owns the APScheduler instance, the subprocesses and the audit rows."""

    def __init__(
        self,
        engine: Engine,
        *,
        settings: Settings | None = None,
        reload_seconds: int = RELOAD_SECONDS,
    ) -> None:
        self._engine = engine
        self._factory: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )
        self._settings = settings or get_settings()
        self._reload_seconds = reload_seconds
        self._states: dict[str, JobState] = {}
        # PID of the process GROUP leader per running job, so SIGTERM can
        # reach the whole tree rather than only the `yfin` wrapper.
        self._groups: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._scheduler: Any = None
        # The database exporter runs in THIS process: it already has an
        # engine, and it is the one part of the stack that is up whether or
        # not anything else is. Built in `run()`, so a `build()`-only test
        # never starts a thread.
        self._exporter: Exporter | None = None

    # --- configuration -----------------------------------------------------

    def crons(self) -> dict[str, str]:
        """The cron expression per job, from the current settings."""
        return {job.name: getattr(self._settings, job.setting, "").strip() for job in JOBS}

    def _trigger(self, cron: str) -> Any:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(cron, timezone=self._settings.yf_schedule_timezone)

    def _grace(self, interval: float) -> int:
        """The misfire grace for a job with this cadence.

        `min(cadence / 2, configured)`. Half the cadence, because a firing
        that is later than that is closer to the NEXT one, and running both
        back to back is worse than dropping the first.
        """
        configured = self._settings.yf_schedule_misfire_grace_seconds
        if interval <= 0:
            return configured
        return max(1, int(min(interval / 2, configured)))

    # --- the job body ------------------------------------------------------

    def _run_job(self, job: Job, scheduled_at: datetime) -> None:
        """Spawn, wait, record. Runs on an executor thread."""
        state = self._states[job.name]
        started = datetime.now(UTC)
        run_id = runs.open_run(self._factory, job.name, scheduled_at)

        env = {**os.environ, JOB_RUN_ID_VAR: str(run_id)}
        try:
            # `start_new_session` puts the child in its own process group,
            # which is what lets SIGTERM reach the shards it spawns rather
            # than only the `yfin` process that started them.
            process = subprocess.Popen(  # noqa: S603 - argv from JOBS, no shell
                [YFIN, *job.command],
                env=env,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - a job that cannot start is a failure
            log.error("job could not start", job=job.name, error=str(exc))
            runs.close_run(
                self._factory, run_id, result="failed", error=f"{type(exc).__name__}: {exc}"
            )
            self._record_result(state, "failed", started)
            return

        with self._lock:
            self._groups[job.name] = process.pid
            state.running = True
        log.info("job started", job=job.name, pid=process.pid, run_id=run_id)

        # The span covers the WAIT, so its duration is the job's. The
        # subprocess draws its own trace and is not a child of this one:
        # the two are separate processes and no context is propagated
        # across the fork, which is honest -- the scheduler's job is to
        # start it and wait, not to be its parent in a trace.
        with tracing.span("scheduler.job", job=job.name) as current:
            try:
                exit_code = process.wait()
            finally:
                with self._lock:
                    self._groups.pop(job.name, None)
                    state.running = False
            # Inside the span, because a span that has ended takes no
            # further attributes -- and `result` is the one thing anybody
            # would filter these traces by.
            #
            # A job the scheduler killed is `terminated`, whatever the
            # shell made of the signal: "we stopped it" and "it failed"
            # are different facts and an operator acts differently on them.
            result = (
                "terminated" if self._stopping.is_set() else runs.result_for(exit_code)
            )
            tracing.set_attributes(current, result=result)

        runs.close_run(self._factory, run_id, result=result, exit_code=exit_code)
        self._record_result(state, result, started)
        log.info("job finished", job=job.name, result=result, exit_code=exit_code)

    def _record_result(self, state: JobState, result: str, started: datetime) -> None:
        state.results[result] = state.results.get(result, 0) + 1
        state.last_duration_seconds = (datetime.now(UTC) - started).total_seconds()
        if result == "ok":
            state.last_success = datetime.now(UTC)
        # A real Prometheus counter, not a gauge the exporter republishes:
        # the scheduler is long-lived, so a monotonic count of its own
        # firings is exactly what a counter is for, and `rate()` over it is
        # what a failing job looks like on a dashboard.
        inc("yfin_job_runs_total", job=state.job.name, result=result)

    # --- what the exporter publishes for us --------------------------------

    def intervals(self) -> dict[str, float]:
        """Each job's mean cadence, in seconds.

        The exporter divides by these to decide what is stale, and it asks
        every pass rather than once: `reload()` can change a trigger sixty
        seconds from now, and freshness judged against a cron nobody runs
        would be wrong in the direction that hides a problem.
        """
        return {name: state.interval_seconds for name, state in self._states.items()}

    def job_samples(self) -> list[Sample]:
        """The job gauges. In this process's memory, and in no table.

        `next_run_timestamp` is the exception -- it comes from APScheduler,
        which is the only thing that knows when a trigger fires next, and it
        is absent for a job with no cron rather than reported as 0. A zero
        there would read as "fires at the epoch", which is overdue by
        fifty-six years.

        `last_success` is seeded from `scheduler_runs` at start-up, so a
        restart does not make every job look overdue at once.
        """
        scheduler = self._scheduler
        samples: list[Sample] = []
        for name, state in self._states.items():
            labels = {"job": name}
            samples.append(Sample("yfin_job_interval_seconds", state.interval_seconds, labels))
            samples.append(Sample("yfin_job_running", float(state.running), labels))
            if state.last_duration_seconds is not None:
                samples.append(
                    Sample("yfin_job_last_duration_seconds", state.last_duration_seconds, labels)
                )
            if state.last_success is not None:
                samples.append(
                    Sample(
                        "yfin_job_last_success_timestamp",
                        state.last_success.timestamp(),
                        labels,
                    )
                )
            job = scheduler.get_job(name) if scheduler is not None else None
            nxt = getattr(job, "next_run_time", None)
            if nxt is not None:
                samples.append(Sample("yfin_job_next_run_timestamp", nxt.timestamp(), labels))
        return samples

    # --- APScheduler wiring ------------------------------------------------

    def _on_event(self, event: Any) -> None:
        """`misfired` and `skipped` never become subprocesses.

        Both are recorded rather than logged. A job that quietly did not run
        is exactly what `scheduler_runs` exists to make visible, and the two
        have different causes: `misfired` means it waited past its grace,
        `skipped` that the previous instance of the same job was still
        going.
        """
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

        if event.code == EVENT_JOB_MISSED:
            result = "misfired"
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            result = "skipped"
        else:  # pragma: no cover - only the two are subscribed
            return
        job_name = str(event.job_id)
        scheduled = getattr(event, "scheduled_run_time", None) or datetime.now(UTC)
        log.warning("job did not run", job=job_name, result=result)
        runs.record_unstarted(self._factory, job_name, scheduled, result)
        state = self._states.get(job_name)
        if state is not None:
            state.results[result] = state.results.get(result, 0) + 1
        # Counted like any other result. A firing that was dropped is the
        # thing `scheduler_runs` and this counter exist to make visible;
        # leaving it out of the metric would put it only in a log line.
        inc("yfin_job_runs_total", job=job_name, result=result)

    def build(self) -> Any:
        """The scheduler, with one trigger per ENABLED job."""
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler(
            executors={
                # One thread, so nothing that talks to Yahoo overlaps.
                "yahoo": ThreadPoolExecutor(1),
                "default": ThreadPoolExecutor(4),
            },
            timezone=self._settings.yf_schedule_timezone,
        )
        scheduler.add_listener(self._on_event, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)

        for job in JOBS:
            cron = self.crons()[job.name]
            state = JobState(job=job, cron=cron)
            self._states[job.name] = state
            if not cron:
                # Empty means NOT REGISTERED, not "registered and disabled":
                # a job with no trigger cannot misfire and cannot be skipped.
                continue
            trigger = self._trigger(cron)
            state.interval_seconds = interval_seconds(trigger)
            scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=job.name,
                name=job.name,
                executor=job.executor,
                args=[job, datetime.now(UTC)],
                max_instances=1,
                # Merges firings the SCHEDULER PROCESS missed, across a
                # restart -- not firings a busy executor queued.
                coalesce=True,
                misfire_grace_time=self._grace(state.interval_seconds),
                replace_existing=True,
            )
        self._scheduler = scheduler
        return scheduler

    # --- reload ------------------------------------------------------------

    def reload(self) -> list[str]:
        """Re-reads the schedule. Returns the jobs whose trigger changed.

        Through `load_overrides` rather than `get_settings()`: the process
        singleton never re-reads by design, and a scheduler that needed a
        restart to pick up a new cron would defeat the reason the crons are
        settings.
        """
        from yfin.core.config import bootstrap_settings, settings_from_overrides
        from yfin.storage import settings_store

        try:
            overrides = settings_store.load_overrides(bootstrap_settings())
        except Exception as exc:  # noqa: BLE001 - a reload failure must not stop the scheduler
            log.warning("schedule reload failed; keeping the current one", error=str(exc))
            return []

        previous_timezone = self._settings.yf_schedule_timezone
        self._settings = settings_from_overrides(overrides)
        changed: list[str] = []
        # A changed timezone moves every trigger, whether or not its
        # expression did.
        rebuild_all = self._settings.yf_schedule_timezone != previous_timezone

        for job in JOBS:
            state = self._states.get(job.name)
            cron = self.crons()[job.name]
            if state is not None and cron == state.cron and not rebuild_all:
                continue
            changed.append(job.name)
            self._apply(job, cron)
        return changed

    def _apply(self, job: Job, cron: str) -> None:
        state = self._states.setdefault(job.name, JobState(job=job, cron=cron))
        state.cron = cron
        scheduler = self._scheduler
        if scheduler is None:  # pragma: no cover - build() runs first
            return
        if not cron:
            if scheduler.get_job(job.name) is not None:
                scheduler.remove_job(job.name)
            state.interval_seconds = 0.0
            log.info("job disabled", job=job.name)
            return
        trigger = self._trigger(cron)
        state.interval_seconds = interval_seconds(trigger)
        if scheduler.get_job(job.name) is None:
            scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=job.name,
                name=job.name,
                executor=job.executor,
                args=[job, datetime.now(UTC)],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=self._grace(state.interval_seconds),
                replace_existing=True,
            )
        else:
            scheduler.reschedule_job(job.name, trigger=trigger)
        log.info("job rescheduled", job=job.name, cron=cron)

    # --- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        """SIGTERM: stop firing, then give the running jobs a bounded wait.

        SIGTERM goes to the process GROUP, so a sync's shard processes get
        it too rather than being orphaned holding the advisory lock. The
        wait is bounded and then escalates, because a scheduler that hangs
        waiting is a scheduler Docker will kill anyway -- and its own grace
        must stay below compose's `stop_grace_period` for that reason.
        """
        self._stopping.set()
        if self._exporter is not None:
            self._exporter.stop()
        if self._scheduler is not None:
            # `wait=False`: the point of the signal is to stop firing NOW;
            # the jobs already running are handled below.
            self._scheduler.shutdown(wait=False)

        with self._lock:
            groups = dict(self._groups)
        for name, pid in groups.items():
            log.info("forwarding SIGTERM to job", job=name, pid=pid)
            self._signal_group(pid, signal.SIGTERM)

        deadline = self._settings.yf_schedule_stop_grace_seconds
        if self._wait_for_jobs(deadline):
            return

        with self._lock:
            groups = dict(self._groups)
        for name, pid in groups.items():
            log.warning("job did not stop in time; killing", job=name, pid=pid)
            self._signal_group(pid, signal.SIGKILL)

    def _wait_for_jobs(self, seconds: int) -> bool:
        """Waits for the running jobs to exit. True if they all did."""
        for _ in range(max(1, seconds * 2)):
            with self._lock:
                if not self._groups:
                    return True
            time.sleep(0.5)
        with self._lock:
            return not self._groups

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError) as exc:
            # It exited between the snapshot and the signal, which is the
            # outcome we wanted anyway.
            log.info("could not signal job process group", pid=pid, error=str(exc))

    # --- run ---------------------------------------------------------------

    def run(self) -> None:
        """Blocks until SIGTERM or SIGINT."""
        scheduler = self.build()
        runs.close_orphans(self._factory)
        for job_name, when in runs.last_success(self._factory).items():
            state = self._states.get(job_name)
            if state is not None:
                state.last_success = when

        def handle(signum: int, _frame: Any) -> None:
            log.info("scheduler stopping", signal=signum)
            self.stop()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

        reloader = threading.Thread(target=self._reload_loop, name="yfin-reload", daemon=True)
        reloader.start()

        self._exporter = Exporter(
            self._engine,
            self._settings,
            intervals=self.intervals,
            job_samples=self.job_samples,
        )
        self._exporter.start()

        log.info(
            "scheduler started",
            jobs=[name for name, cron in self.crons().items() if cron],
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):  # pragma: no cover - signal path
            self.stop()

    def _reload_loop(self) -> None:
        while not self._stopping.wait(self._reload_seconds):
            changed = self.reload()
            if changed:
                log.info("schedule reloaded", changed=changed)


__all__ = ["RELOAD_SECONDS", "YFIN", "JobState", "SchedulerService"]
