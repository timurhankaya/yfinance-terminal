"""`yfin changes` refuses to pretend when the feature is off: a `status` printing
"0 rows unpublished" with publishing disabled would read as healthy."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from yfin.cli.app import app
from yfin.cli.changes import changes_app
from yfin.core.config import Settings


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing off, which is the default."""
    monkeypatch.setattr(
        "yfin.core.config.get_settings",
        lambda: Settings(yf_changes_enabled=False),  # type: ignore[call-arg]
    )


def test_the_group_is_mounted(cli: CliRunner) -> None:
    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "changes" in result.stdout


def test_it_offers_relay_and_status(cli: CliRunner) -> None:
    result = cli.invoke(changes_app, ["--help"])
    assert result.exit_code == 0
    assert "relay" in result.stdout
    assert "status" in result.stdout


class TestRefusalWhenOff:
    """Exit 1, like `yfin stream relay` does for `yf_kafka_enabled`."""

    def test_relay_exits_one_and_says_how_to_enable(
        self, cli: CliRunner, off: None
    ) -> None:
        result = cli.invoke(changes_app, ["relay"])
        assert result.exit_code == 1
        assert "yf_changes_enabled" in result.stdout
        assert "yfin config set" in result.stdout

    def test_status_exits_one_rather_than_reporting_zero(
        self, cli: CliRunner, off: None
    ) -> None:
        """Zero unpublished rows with publishing off is not health, it is
        the absence of the feature -- and printing it as a number would
        read as the former."""
        result = cli.invoke(changes_app, ["status"])
        assert result.exit_code == 1
        assert "yf_changes_enabled" in result.stdout


def test_the_setting_is_off_by_default() -> None:
    """Off means the writer emits the statements it emitted before any of
    this existed, so the feature costs nothing until someone opts in."""
    assert Settings.model_fields["yf_changes_enabled"].default is False
