"""The scheduler's decisions, without starting one.

What is testable here is everything that decides WHETHER and HOW a job runs:
the job table, the cadence a cron implies, the misfire grace derived from
it, and the mapping from an exit code to a word. The subprocess and the
signal handling need a real process and live in the repo tests.
"""

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
    """The mean period, not the gap to the next firing.

    For `0 4 1 * *` the gap is anywhere from one day to thirty-one depending
    on when you ask, and both the misfire grace and the freshness factor are
    derived from it.
    """

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
