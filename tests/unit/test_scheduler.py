"""The scheduler's decisions, without starting one: the job table, the cadence a cron
implies, the misfire grace derived from it, and exit code to word. The subprocess and
signal handling live in the repo tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yfin.core.config import SETTING_GROUPS, Settings
from yfin.scheduler.jobs import JOBS, JOBS_BY_NAME, interval_seconds
from yfin.scheduler.runs import result_for

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _trigger(cron: str) -> object:
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger.from_crontab(cron, timezone="UTC")


class TestTheJobTable:
    def test_every_job_names_a_real_setting(self) -> None:
        for job in JOBS:
            assert job.setting in Settings.model_fields, job.name

    def test_every_setting_is_in_the_scheduler_group(self) -> None:
        assert "scheduler" in SETTING_GROUPS
        for job in JOBS:
            extra = Settings.model_fields[job.setting].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["group"] == "scheduler", job.name

    def test_names_are_unique(self) -> None:
        assert len(JOBS_BY_NAME) == len(JOBS)

    def test_everything_that_touches_yahoo_shares_one_queue(self) -> None:
        """A single-threaded executor is the only thing stopping two jobs
        from splitting one IP's rate budget, or from taking the sync
        advisory lock at once."""
        yahoo = {job.name for job in JOBS if job.executor == "yahoo"}
        assert yahoo == {"sync", "market", "domain", "stream_reconcile"}

    def test_nothing_else_is_serialised(self) -> None:
        """`prune`, `bars maintain` and `usage flush` have no upstream and
        no lock between them; queueing them behind a three-hour sync would
        cost availability for nothing."""
        default = {job.name for job in JOBS if job.executor == "default"}
        assert default == {"bars_maintain", "prune", "usage_flush"}

    def test_no_job_runs_a_shell(self) -> None:
        """Every command is argv, so nothing an operator types into a
        setting can become a shell word."""
        for job in JOBS:
            assert isinstance(job.command, tuple)
            for part in job.command:
                assert " " not in part, job.name


class TestInterval:
    """The mean period, not the gap to the next firing: for `0 4 1 * *` the gap is anywhere
    from one to thirty-one days, and misfire grace and freshness factor derive from it."""

    @pytest.mark.parametrize(
        ("cron", "expected_hours"),
        [
            ("15 * * * *", 1),
            ("0 2 * * *", 24),
            ("0 3 * * 0", 24 * 7),
            ("0 4 1 * *", 24 * 30.4),  # the mean month, not a nominal 30
        ],
    )
    def test_it_matches_the_cadence(self, cron: str, expected_hours: float) -> None:
        hours = interval_seconds(_trigger(cron), now=NOW) / 3600
        assert hours == pytest.approx(expected_hours, rel=0.05)

    def test_it_does_not_depend_on_when_it_is_asked(self) -> None:
        """A monthly job asked on the 2nd and on the 28th must report the
        same cadence, or its grace would swing with the calendar."""
        trigger = _trigger("0 4 1 * *")
        second = interval_seconds(trigger, now=datetime(2026, 9, 2, tzinfo=UTC))
        late = interval_seconds(trigger, now=datetime(2026, 9, 28, tzinfo=UTC))
        assert second == pytest.approx(late, rel=0.05)

    def test_a_trigger_that_never_fires_has_no_cadence(self) -> None:
        """Read as "no cadence" by the callers rather than divided by."""
        from apscheduler.triggers.cron import CronTrigger

        never = CronTrigger(
            year=2020, month=1, day=1, hour=0, minute=0, timezone="UTC"
        )
        assert interval_seconds(never, now=NOW) == 0.0


class TestMisfireGrace:
    """`min(cadence / 2, configured)`.

    Half the cadence, because a firing later than that is closer to the NEXT
    one, and running both back to back is worse than dropping the first.
    """

    def _grace(self, cron: str, configured: int = 3600) -> int:
        from yfin.scheduler.service import SchedulerService

        service = SchedulerService.__new__(SchedulerService)
        service._settings = Settings(  # type: ignore[attr-defined]
            yf_schedule_misfire_grace_seconds=configured
        )
        return service._grace(interval_seconds(_trigger(cron), now=NOW))  # type: ignore[attr-defined]

    def test_an_hourly_job_does_not_accept_a_fifty_minute_delay(self) -> None:
        assert self._grace("15 * * * *") == pytest.approx(1800, rel=0.05)

    def test_a_daily_job_is_capped_by_the_setting(self) -> None:
        """Half a day is twelve hours; the configured hour wins."""
        assert self._grace("0 2 * * *") == 3600

    def test_the_setting_can_lower_it_further(self) -> None:
        assert self._grace("0 2 * * *", configured=60) == 60

    def test_it_is_never_zero(self) -> None:
        """A grace of 0 would drop every firing that was not instantaneous."""
        assert self._grace("15 * * * *", configured=0) >= 1


class TestResultForExitCode:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [(0, "ok"), (2, "partial"), (4, "locked")],
    )
    def test_the_defined_codes(self, code: int, expected: str) -> None:
        assert result_for(code) == expected

    @pytest.mark.parametrize("code", [1, 3, 5, 6, 127, 255])
    def test_everything_else_is_a_failure(self, code: int) -> None:
        """Unmapped rather than enumerated: a code nobody planned for is a
        failure by definition, and listing 1, 3 and 5 would leave a future
        6 silently reported as ok."""
        assert result_for(code) == "failed"

    def test_locked_is_not_a_failure(self) -> None:
        """It is the expected outcome of an overlap: the previous run is
        still going and the advisory lock did its job."""
        assert result_for(4) != "failed"


class TestCrons:
    def test_an_empty_expression_disables_the_job(self) -> None:
        from yfin.scheduler.service import SchedulerService

        service = SchedulerService.__new__(SchedulerService)
        service._settings = Settings(yf_schedule_prune="")  # type: ignore[attr-defined]
        assert service.crons()["prune"] == ""  # type: ignore[attr-defined]

    def test_whitespace_only_also_disables_it(self) -> None:
        """`yfin config set yf_schedule_prune " "` must not register a job
        whose cron is a space."""
        from yfin.scheduler.service import SchedulerService

        service = SchedulerService.__new__(SchedulerService)
        service._settings = Settings.model_construct(yf_schedule_prune="   ")  # type: ignore[attr-defined]
        assert service.crons()["prune"] == ""  # type: ignore[attr-defined]


class TestTheJobGauges:
    """What the exporter publishes on the scheduler's behalf. These numbers live in process
    memory, not a table, which is why the exporter takes a callable and absence matters."""

    def _service(self, **state: object) -> object:
        from yfin.scheduler.service import JobState, SchedulerService

        service = SchedulerService.__new__(SchedulerService)
        service._scheduler = None  # type: ignore[attr-defined]
        service._states = {  # type: ignore[attr-defined]
            "sync": JobState(job=JOBS_BY_NAME["sync"], cron="0 2 * * *", **state)  # type: ignore[arg-type]
        }
        return service

    def _samples(self, **state: object) -> dict[str, float]:
        service = self._service(**state)
        return {s.name: s.value for s in service.job_samples()}  # type: ignore[attr-defined]

    def test_a_job_that_has_never_run_still_reports_its_cadence(self) -> None:
        """Otherwise a job whose cron was just set would be invisible until
        its first firing, which for `prune` is a month."""
        samples = self._samples(interval_seconds=86400.0)
        assert samples["yfin_job_interval_seconds"] == 86400.0
        assert samples["yfin_job_running"] == 0.0

    def test_a_job_that_has_never_succeeded_reports_no_timestamp(self) -> None:
        """A 0 there would read as "last succeeded at the epoch", which is
        overdue by fifty-six years and would fire `JobOverdue` on every
        job the day it is added."""
        assert "yfin_job_last_success_timestamp" not in self._samples()
        assert "yfin_job_last_duration_seconds" not in self._samples()

    def test_a_finished_job_reports_both(self) -> None:
        when = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
        samples = self._samples(last_success=when, last_duration_seconds=91.5)
        assert samples["yfin_job_last_success_timestamp"] == when.timestamp()
        assert samples["yfin_job_last_duration_seconds"] == 91.5

    def test_a_running_job_says_so(self) -> None:
        assert self._samples(running=True)["yfin_job_running"] == 1.0

    def test_no_next_run_without_a_scheduler(self) -> None:
        """`build()` has not run, so nothing knows when the trigger fires."""
        assert "yfin_job_next_run_timestamp" not in self._samples()

    def test_the_next_firing_comes_from_apscheduler(self) -> None:
        """It is the only thing that knows, and a job with no cron has none
        rather than a zero that would read as the epoch."""

        class _Job:
            next_run_time = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)

        class _Scheduler:
            @staticmethod
            def get_job(name: str) -> object:
                return _Job() if name == "sync" else None

        service = self._service()
        service._scheduler = _Scheduler()  # type: ignore[attr-defined]
        samples = {s.name: s.value for s in service.job_samples()}  # type: ignore[attr-defined]
        assert samples["yfin_job_next_run_timestamp"] == _Job.next_run_time.timestamp()

    def test_every_gauge_it_publishes_is_declared(self) -> None:
        from yfin.core.metrics import METRICS

        service = self._service(last_success=NOW, last_duration_seconds=1.0)
        for sample in service.job_samples():  # type: ignore[attr-defined]
            spec = METRICS[sample.name]
            assert spec.kind == "gauge"
            assert set(sample.labels) == set(spec.labelnames)

    def test_the_cadences_are_what_the_exporter_divides_by(self) -> None:
        service = self._service(interval_seconds=3600.0)
        assert service.intervals() == {"sync": 3600.0}  # type: ignore[attr-defined]


class TestEveryScheduledCommandMapsTheLock:
    """A job that collided with another must be `locked`, not `failed`: the alerts read the
    difference, and letting `LockNotAcquired` reach the generic handler would fire
    SyncFailed and JobPartial for a normal overlap."""

    def test_the_mapping_reserves_a_code_for_it(self) -> None:
        from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED

        assert result_for(EXIT_LOCK_NOT_ACQUIRED) == "locked"
        assert result_for(EXIT_LOCK_NOT_ACQUIRED) != "failed"

    def test_every_locking_command_translates_it(self) -> None:
        """Static, because the alternative is a repo test that has to hold
        the lock from another connection for each of them."""
        import inspect

        from yfin.cli import app, domain, market, stream

        for module in (app, domain, market, stream):
            source = inspect.getsource(module)
            if "advisory_lock" not in source and "run_sync" not in source:
                continue
            assert "LockNotAcquired" in source, module.__name__
            assert "EXIT_LOCK_NOT_ACQUIRED" in source, module.__name__

    def test_reconcile_catches_it_where_it_takes_the_lock(self) -> None:
        """The one that did not, until a live run showed it."""
        import inspect

        from yfin.cli.stream import stream_reconcile

        source = inspect.getsource(stream_reconcile)
        assert "except LockNotAcquired" in source
        assert "EXIT_LOCK_NOT_ACQUIRED" in source
