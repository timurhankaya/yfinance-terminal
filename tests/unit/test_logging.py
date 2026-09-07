"""One pipeline, two renderings, and the redaction that has to survive both.

The claim under test is not "logging works". It is that there is exactly
ONE route out of this process and every record takes it -- because
redaction is a processor, and a record that went around the chain is a
record nobody redacted. yfinance is handed the proxy DSN in plain text, so
that is not a hypothetical.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from yfin.core.logging_setup import (
    bridge_yfinance_logging,
    configure_logging,
    get_logger,
    redact_credentials,
    redact_secrets,
    scrub,
)

DSN = "socks5h://acct:s3cret@10.0.0.1:1080"
SECRET = "s3cret"  # noqa: S105 - the thing that must not appear in a line


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """No test inherits another's handlers or service name.

    `configure_logging` is process state twice over -- the root handler and
    the module-level service -- so a test that reconfigured would otherwise
    decide what the next one sees.
    """
    from yfin.core import logging_setup

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    service = logging_setup._service
    yield
    structlog.reset_defaults()
    root.handlers = handlers
    root.setLevel(level)
    logging_setup._service = service


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


def _json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    lines = _lines(capsys)
    assert len(lines) == 1, lines
    return json.loads(lines[0])  # type: ignore[no-any-return]


class TestTheFixedFields:
    def test_a_line_carries_them_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("started")
        line = _json_line(capsys)
        assert line["event"] == "started"
        assert line["level"] == "info"
        assert line["logger"] == "probe"
        assert line["service"] == "sync"
        assert line["timestamp"].endswith("Z")

    def test_the_timestamp_is_utc_iso(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Loki sorts by ingestion time; the line's own clock is what a
        reader correlates against a run, and a local one would be a
        different number on every host."""
        from datetime import datetime

        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("x")
        stamp = _json_line(capsys)["timestamp"]
        assert datetime.fromisoformat(stamp).tzinfo is not None

    def test_bound_context_reaches_the_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("INFO", "json", "sync")
        structlog.contextvars.bind_contextvars(run_id=7, shard=2)
        try:
            get_logger("probe").info("x")
        finally:
            structlog.contextvars.clear_contextvars()
        line = _json_line(capsys)
        assert line["run_id"] == 7
        assert line["shard"] == 2


class TestTheService:
    def test_it_is_not_a_contextvar(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`ThreadPoolExecutor` does not copy the context into its workers
        -- `bind_shard_context` exists because of it -- so a `service` bound
        with `bind_contextvars` would be missing from exactly the fetch and
        normalise lines a dashboard filters by service to find."""
        from concurrent.futures import ThreadPoolExecutor

        configure_logging("INFO", "json", "sync")
        with ThreadPoolExecutor(1) as pool:
            pool.submit(get_logger("worker").info, "in a thread").result()
        assert _json_line(capsys)["service"] == "sync"

    def test_a_reconfigure_without_one_keeps_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`create_app` reconfigures for the format; it must not blank the
        name the entry point already set."""
        configure_logging("INFO", "json", "api")
        configure_logging("INFO", "json")
        get_logger("probe").info("x")
        assert _json_line(capsys)["service"] == "api"

    def test_a_subprocess_can_claim_its_own(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A shard is `sync`, not the scheduler that started it."""
        configure_logging("INFO", "json", "scheduler")
        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("x")
        assert _json_line(capsys)["service"] == "sync"


class TestTheFormat:
    def test_json_is_one_object_per_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("first")
        get_logger("probe").info("second")
        assert [json.loads(line)["event"] for line in _lines(capsys)] == [
            "first",
            "second",
        ]

    def test_console_is_not_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", "console", "sync")
        get_logger("probe").info("started", run_id=4)
        (line,) = _lines(capsys)
        assert "started" in line
        assert "run_id" in line
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_an_unknown_format_falls_back(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A mistyped `LOG_FORMAT` must not stop a sync."""
        configure_logging("INFO", "yaml", "sync")
        get_logger("probe").info("x")
        assert _lines(capsys)


class TestOnlyOneHandler:
    def test_reconfiguring_does_not_double_the_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every CLI command configures, and so does `create_app`. A second
        handler would print every line twice for the rest of the process."""
        configure_logging("INFO", "json", "sync")
        configure_logging("INFO", "json", "sync")
        configure_logging("DEBUG", "console", "sync")
        get_logger("probe").info("once")
        assert len(_lines(capsys)) == 1

    def test_the_level_is_honoured(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("WARNING", "json", "sync")
        get_logger("probe").info("dropped")
        get_logger("probe").warning("kept")
        assert [json.loads(line)["event"] for line in _lines(capsys)] == ["kept"]

    def test_logs_go_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`config export`, `config schema --json` and `dump_openapi.py`
        write machine-readable JSON to stdout, and logging is configured
        before they run."""
        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("x")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "x" in captured.err


class TestForeignRecords:
    """SQLAlchemy, uvicorn and yfinance never touch structlog."""

    def test_a_stdlib_record_is_rendered_by_the_same_chain(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("INFO", "json", "sync")
        logging.getLogger("sqlalchemy.engine").warning("slow statement")
        line = _json_line(capsys)
        assert line["event"] == "slow statement"
        assert line["logger"] == "sqlalchemy.engine"
        assert line["service"] == "sync"

    def test_a_stdlib_record_is_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The point of the whole re-plumb. yfinance is handed the DSN in
        plain text and logs it at DEBUG."""
        configure_logging("INFO", "json", "sync")
        logging.getLogger("yfinance").warning("using proxy %s", DSN)
        line = _json_line(capsys)
        assert SECRET not in json.dumps(line)
        assert "acct:***@" in line["event"]

    def test_console_redacts_a_foreign_record_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Redaction is a processor in the SHARED chain, so it cannot
        depend on which renderer is installed."""
        configure_logging("INFO", "console", "sync")
        logging.getLogger("yfinance").warning("using proxy %s", DSN)
        (line,) = _lines(capsys)
        assert SECRET not in line


class TestTheYfinanceBridge:
    def test_it_leaves_a_handler_so_yfinance_adds_none(self) -> None:
        """`yf.config.debug.logging = True` runs `_enable_debug_mode()`,
        which installs its own unredacted `StreamHandler` on a handler-less
        logger. The NullHandler is a decoy that makes that check pass."""
        logger = logging.getLogger("yfinance")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        bridge_yfinance_logging()
        assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)

    def test_it_propagates_to_root(self) -> None:
        """Which is the only thing that carries the record to the shared
        chain. The old bridge set propagate to False and re-emitted through
        a second code path that could drift from this one."""
        bridge_yfinance_logging()
        assert logging.getLogger("yfinance").propagate is True

    def test_it_is_idempotent(self) -> None:
        logger = logging.getLogger("yfinance")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        bridge_yfinance_logging()
        bridge_yfinance_logging()
        nulls = [h for h in logger.handlers if isinstance(h, logging.NullHandler)]
        assert len(nulls) == 1

    def test_a_bridged_record_appears_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A handler that both emitted and propagated would print twice."""
        configure_logging("INFO", "json", "sync")
        bridge_yfinance_logging()
        logging.getLogger("yfinance").warning("hello")
        assert len(_lines(capsys)) == 1


class TestTracebacks:
    def test_a_local_variable_does_not_leak(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`show_locals` off, and this is why. The default serialises every
        frame local into the traceback, and the redaction processors only
        ever see top-level event keys -- so a DSN held in a local would
        travel to Loki untouched."""

        def deeper() -> None:
            dsn = DSN  # noqa: F841 - the local this test is about
            raise RuntimeError("upstream refused")

        configure_logging("INFO", "json", "sync")
        try:
            deeper()
        except RuntimeError:
            get_logger("probe").exception("fetch failed")

        rendered = "\n".join(_lines(capsys))
        assert "upstream refused" in rendered
        assert SECRET not in rendered

    def test_the_console_traceback_does_not_leak_either(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def deeper() -> None:
            dsn = DSN  # noqa: F841 - the local this test is about
            raise RuntimeError("upstream refused")

        configure_logging("INFO", "console", "sync")
        try:
            deeper()
        except RuntimeError:
            get_logger("probe").exception("fetch failed")

        rendered = "\n".join(_lines(capsys))
        assert "upstream refused" in rendered
        assert SECRET not in rendered

    def test_a_json_traceback_is_data(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A rendered string would be one enormous unsearchable field;
        Grafana can expand a list of frames."""
        configure_logging("INFO", "json", "sync")
        try:
            raise ValueError("boom")
        except ValueError:
            get_logger("probe").exception("failed")
        line = _json_line(capsys)
        assert line["exception"][0]["exc_type"] == "ValueError"


class TestTraceContext:
    def test_nothing_is_added_without_a_span(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Not zeros. An all-zero id is what OpenTelemetry returns for the
        invalid span, and Grafana would turn it into a link to a trace that
        does not exist."""
        configure_logging("INFO", "json", "sync")
        get_logger("probe").info("x")
        line = _json_line(capsys)
        assert "trace_id" not in line
        assert "span_id" not in line

    def test_it_is_a_no_op_without_the_extra(self) -> None:
        """`[otel]` is optional, and a missing extra must not stop a run."""
        from yfin.core.logging_setup import _add_trace_context

        assert _add_trace_context(None, "info", {"event": "x"}) == {"event": "x"}


class TestRedactionProcessors:
    """The unit-level contract the chain above depends on."""

    def test_scrub_hides_the_password(self) -> None:
        assert scrub(DSN) == "socks5h://acct:***@10.0.0.1:1080"

    def test_scrub_leaves_a_plain_url_alone(self) -> None:
        url = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL"
        assert scrub(url) == url

    def test_a_secret_key_is_replaced_whatever_its_value(self) -> None:
        event = redact_secrets(None, "info", {"authorization": "Bearer live", "n": 1})
        assert event["authorization"] == "***"
        assert event["n"] == 1

    def test_a_credential_anywhere_in_the_event_is_scrubbed(self) -> None:
        event = redact_credentials(None, "info", {"proxy": DSN})
        assert SECRET not in event["proxy"]


class TestTraceContextWithASpan:
    """Only runs with the `[otel]` extra; skipped without it, which is the
    same shape the `kafka` extra's tests use."""

    def test_a_line_inside_a_span_carries_its_ids(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """This is what makes a log line clickable through to a trace:
        Grafana's `derivedFields` matches `trace_id` in the line body, not
        a label, so the id has to be IN the JSON."""
        sdk = pytest.importorskip("opentelemetry.sdk.trace")
        from opentelemetry import trace

        provider = sdk.TracerProvider()
        previous = trace.get_tracer_provider()
        trace._TRACER_PROVIDER = provider  # noqa: SLF001 - no public reset
        try:
            configure_logging("INFO", "json", "sync")
            with provider.get_tracer("test").start_as_current_span("probe"):
                get_logger("probe").info("inside")
            line = _json_line(capsys)
        finally:
            trace._TRACER_PROVIDER = previous  # noqa: SLF001

        assert len(line["trace_id"]) == 32
        assert len(line["span_id"]) == 16
        assert int(line["trace_id"], 16) != 0
