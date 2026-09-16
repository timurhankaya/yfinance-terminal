"""Job definitions are settings, so a bad cron must be refused at `yfin config set` rather
than hours later in the scheduler's logs. A `field_validator` makes that true:
`validate_pair` builds a `Settings` from the candidate, so CLI, loader and scheduler share
the check."""

from __future__ import annotations

import builtins
from typing import Any

import pytest
from pydantic import ValidationError

from yfin.core.config import ENV_ONLY_FIELDS, SETTING_GROUPS, Settings

SCHEDULE_FIELDS = [
    name for name in Settings.model_fields if name.startswith("yf_schedule_")
]
CRON_FIELDS = [
    name
    for name in SCHEDULE_FIELDS
    if not name.endswith(("_seconds", "_timezone"))
]


class TestTheGroups:
    def test_both_new_groups_exist(self) -> None:
        assert "scheduler" in SETTING_GROUPS
        assert "monitoring" in SETTING_GROUPS

    def test_every_schedule_setting_is_in_the_scheduler_group(self) -> None:
        for name in SCHEDULE_FIELDS:
            extra = Settings.model_fields[name].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["group"] == "scheduler", name

    def test_the_monitoring_group_is_populated(self) -> None:
        groups = [
            (Settings.model_fields[n].json_schema_extra or {}).get("group")  # type: ignore[union-attr]
            for n in Settings.model_fields
        ]
        assert "monitoring" in groups


class TestCronValidation:
    @pytest.mark.parametrize("field", CRON_FIELDS)
    def test_every_cron_field_is_validated(self, field: str) -> None:
        """A field that looks like a cron but is not checked is the one that
        will be set wrong."""
        with pytest.raises(ValidationError):
            Settings(**{field: "not a cron"})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "expression", ["0 2 * * *", "*/15 * * * *", "0 4 1 * *", "0 3 * * 0"]
    )
    def test_the_shipped_defaults_are_accepted(self, expression: str) -> None:
        assert Settings(yf_schedule_sync=expression).yf_schedule_sync == expression

    @pytest.mark.parametrize(
        "expression",
        [
            "not a cron",
            "0 2 * *",  # four fields
            "0 2 * * * *",  # six
            "99 2 * * *",  # minute out of range
            "* * * * 9",  # day-of-week out of range
        ],
    )
    def test_a_bad_expression_is_refused(self, expression: str) -> None:
        with pytest.raises(ValidationError):
            Settings(yf_schedule_sync=expression)

    def test_empty_is_how_a_job_is_switched_off(self) -> None:
        assert Settings(yf_schedule_sync="").yf_schedule_sync == ""

    def test_prune_ships_switched_off(self) -> None:
        """Irreversible: a deleted row comes back from no source."""
        assert Settings().yf_schedule_prune == ""

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert Settings(yf_schedule_sync="  0 2 * * *  ").yf_schedule_sync == "0 2 * * *"


class TestWithoutTheSchedulerExtra:
    """`core/config` is imported by everything, so it must not need
    APScheduler. Without it the check counts fields, which catches the typo
    that actually happens."""

    @pytest.fixture
    def no_apscheduler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = builtins.__import__

        def fake(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("apscheduler"):
                raise ImportError("no apscheduler")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake)

    def test_a_valid_expression_still_passes(self, no_apscheduler: None) -> None:
        assert Settings(yf_schedule_sync="0 2 * * *").yf_schedule_sync == "0 2 * * *"

    def test_the_wrong_number_of_fields_is_still_caught(
        self, no_apscheduler: None
    ) -> None:
        with pytest.raises(ValidationError, match="five fields"):
            Settings(yf_schedule_sync="0 2 * *")

    def test_empty_still_means_off(self, no_apscheduler: None) -> None:
        assert Settings(yf_schedule_sync="").yf_schedule_sync == ""


class TestEnvOnlyFields:
    def test_the_metrics_port_cannot_be_a_database_setting(self) -> None:
        """One value in the settings table would bind five services to one
        port; each gets its own from the compose override."""
        assert "metrics_port" in ENV_ONLY_FIELDS

    def test_the_log_format_cannot_be_a_database_setting(self) -> None:
        """Decided before the first log line, which is before any database
        exists -- the same argument `log_level` already makes."""
        assert "log_format" in ENV_ONLY_FIELDS

    def test_metrics_are_off_by_default(self) -> None:
        assert Settings().metrics_port == 0
