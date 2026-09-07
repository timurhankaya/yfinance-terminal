"""Runner: settings mapping and the refusal to start when disabled.

The parts that need a database (the advisory lock, the session row) are
covered in tests/repo.
"""

from __future__ import annotations

import pytest

from yfin.core.config import Settings
from yfin.stream.runner import (
    EXIT_OK,
    EXIT_WRITER_FAILED,
    StreamDisabled,
    canary_symbols,
    supervisor_config,
    writer_config,
)


def _settings(**overrides: object) -> Settings:
    return Settings().model_copy(update=overrides)


# --- the master switch -----------------------------------------------------


def test_disabled_stream_refuses_to_start() -> None:
    from yfin.stream.runner import run_stream

    with pytest.raises(StreamDisabled, match="yf_stream_enabled"):
        run_stream(None, _settings(yf_stream_enabled=False))  # type: ignore[arg-type]


def test_the_refusal_says_how_to_enable_it() -> None:
    from yfin.stream.runner import run_stream

    with pytest.raises(StreamDisabled, match="yfin config set"):
        run_stream(None, _settings(yf_stream_enabled=False))  # type: ignore[arg-type]


# --- canary ----------------------------------------------------------------


def test_canary_defaults_to_one_symbol() -> None:
    assert canary_symbols(_settings()) == ("BTC-USD",)


def test_canary_list_is_split_and_normalised() -> None:
    settings = _settings(yf_stream_canary_symbols=" btc-usd , eth-usd ")
    assert canary_symbols(settings) == ("BTC-USD", "ETH-USD")


def test_empty_canary_setting_yields_none() -> None:
    """Allowed: the idle watchdog still covers silence, just less sharply."""
    assert canary_symbols(_settings(yf_stream_canary_symbols="")) == ()


def test_canary_count_shrinks_the_usable_quota() -> None:
    """Two canaries mean two fewer real symbols per connection."""
    settings = _settings(yf_stream_canary_symbols="BTC-USD,ETH-USD")
    config = supervisor_config(settings)
    assert len(config.canary_symbols) == 2
    assert config.max_symbols_per_connection + len(config.canary_symbols) <= 100


# --- settings mapping ------------------------------------------------------


def test_supervisor_config_reads_the_settings() -> None:
    settings = _settings(
        yf_stream_max_connections=12,
        yf_stream_max_symbols_per_connection=42,
        yf_stream_queue_maxsize=555,
        yf_stream_rescan_seconds=15,
    )
    config = supervisor_config(settings)
    assert config.max_connections == 12
    assert config.max_symbols_per_connection == 42
    assert config.queue_maxsize == 555
    assert config.rescan_seconds == 15.0


def test_writer_config_reads_the_settings() -> None:
    settings = _settings(
        yf_stream_batch_size=7,
        yf_stream_batch_interval_ms=99,
        yf_stream_quotes_every_n_batches=3,
        yf_stream_reject_sample_per_hour=5,
    )
    config = writer_config(settings)
    assert config.batch_size == 7
    assert config.batch_interval_ms == 99
    assert config.quotes_every_n_batches == 3
    assert config.reject_sample_per_hour == 5


def test_defaults_respect_yahoos_quota() -> None:
    """The setting is bounded at 99 so it can never reach the limit alone,
    and the default leaves room for the canary."""
    config = supervisor_config(_settings())
    assert config.max_symbols_per_connection + len(config.canary_symbols) <= 100


@pytest.mark.parametrize("value", [0, 100, 200])
def test_per_connection_size_is_range_checked(value: int) -> None:
    """Yahoo truncates silently past 100, so the setting cannot express it."""
    with pytest.raises(ValueError):
        Settings(yf_stream_max_symbols_per_connection=value)  # type: ignore[arg-type]


# --- exit codes ------------------------------------------------------------


def test_exit_codes_are_distinct() -> None:
    """A dead writer must not look like a clean stop: the process was up
    while collecting nothing, and a supervisor needs to see that."""
    assert EXIT_OK == 0
    assert EXIT_WRITER_FAILED != EXIT_OK
