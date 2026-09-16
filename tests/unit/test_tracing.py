"""Tracing is optional twice over and must cost nothing when off: without the `[otel]`
extra every function is a no-op that imports nothing, and without
`OTEL_EXPORTER_OTLP_ENDPOINT` it stays off so an uncollected process does not build spans
it will throw away."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from yfin.core import tracing


def _code(function: object) -> str:
    """A function's source with comments and docstring stripped: the strings the assertions
    look for are named in prose next to the line that avoids them."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(function).lstrip())
    body = tree.body[0]
    assert isinstance(body, ast.FunctionDef)
    if ast.get_docstring(body) is not None:
        body.body = body.body[1:]
    return ast.unparse(body)


@pytest.fixture(autouse=True)
def _off() -> Iterator[None]:
    """`_enabled` is process state; no test inherits another's provider."""
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()


class TestWhenItIsOff:
    def test_an_empty_endpoint_leaves_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off is the default everywhere, so this is not an error and not
        even a warning -- a line about it on every CLI invocation would be
        noise on the one path that runs thousands of times a day."""
        monkeypatch.delenv(tracing.ENDPOINT_VAR, raising=False)
        assert tracing.configure_tracing("sync") is False
        assert tracing.enabled() is False

    def test_whitespace_is_not_an_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`OTEL_EXPORTER_OTLP_ENDPOINT=` in a compose file arrives as an
        empty string, and a stray space in one arrives as a space."""
        monkeypatch.setenv(tracing.ENDPOINT_VAR, "   ")
        assert tracing.configure_tracing("sync") is False

    def test_a_span_is_a_no_op(self) -> None:
        with tracing.span("sync.symbol", symbol="AAPL") as current:
            assert current is None

    def test_a_span_still_runs_its_block(self) -> None:
        seen = []
        with tracing.span("sync.symbol"):
            seen.append(1)
        assert seen == [1]

    def test_a_span_does_not_swallow_an_exception(self) -> None:
        """An error boundary that silently ate errors would be the worst
        possible thing to bolt onto a pipeline."""
        with pytest.raises(ValueError, match="boom"), tracing.span("sync.symbol"):
            raise ValueError("boom")

    def test_attributes_on_a_no_op_span_are_ignored(self) -> None:
        tracing.set_attributes(None, rows_written=17)

    def test_a_failing_attribute_does_not_break_the_block(self) -> None:
        """Instrumentation that can fail turns a working sync into a broken one. Not under
        `TestWhenItIsOn`: the hostile object supplies the span interface itself, so this
        runs without the `[otel]` extra, where a raising `set_attributes` matters most."""

        class _Hostile:
            def set_attribute(self, *_args: object) -> None:
                raise RuntimeError("nope")

        tracing.set_attributes(_Hostile(), rows_written=1)

    def test_instrumenting_does_nothing(self) -> None:
        tracing.instrument_fastapi(object())
        tracing.instrument_sqlalchemy(object())

    def test_it_imports_no_opentelemetry(self) -> None:
        """The module is imported by `pipeline/persist.py`, which runs in
        every shard. Importing the SDK there would cost a hundred
        milliseconds per process to do nothing."""
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-c",
                "import sys; import yfin.core.tracing; "
                "print(any(m.startswith('opentelemetry.sdk') for m in sys.modules))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"


class TestWhenItIsOn:
    """Skipped without the extra, the same shape the `kafka` tests use."""

    @pytest.fixture
    def spans(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        sdk = pytest.importorskip("opentelemetry.sdk.trace")
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider = sdk.TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
        # The endpoint is what `configure_tracing` reads, but the provider
        # above is already installed -- so the flag is set directly rather
        # than building a second provider that would export to a collector
        # this test does not have.
        monkeypatch.setattr(tracing, "_enabled", True)
        return exporter.get_finished_spans  # type: ignore[return-value]

    def test_a_span_carries_its_attributes(self, spans: object) -> None:
        with tracing.span("sync.symbol", symbol="AAPL", dataset_count=3):
            pass
        (recorded,) = spans()  # type: ignore[operator]
        assert recorded.name == "sync.symbol"
        assert recorded.attributes["symbol"] == "AAPL"
        assert recorded.attributes["dataset_count"] == 3

    def test_a_none_attribute_is_left_off(self, spans: object) -> None:
        """`None` is not a value OTel can carry, and an attribute set to
        the string "None" would be worse than an absent one."""
        with tracing.span("sync.symbol", symbol="AAPL", proxy=None):
            pass
        (recorded,) = spans()  # type: ignore[operator]
        assert "proxy" not in recorded.attributes

    def test_an_attribute_can_be_added_at_the_end(self, spans: object) -> None:
        """`rows_written` does not exist when the span opens; that is the
        whole reason the span is around the loop rather than inside it."""
        with tracing.span("sync.symbol", symbol="AAPL") as current:
            tracing.set_attributes(current, rows_written=42)
        (recorded,) = spans()  # type: ignore[operator]
        assert recorded.attributes["rows_written"] == 42

    def test_an_exception_still_ends_the_span(self, spans: object) -> None:
        with pytest.raises(ValueError, match="boom"), tracing.span("sync.symbol"):
            raise ValueError("boom")
        (recorded,) = spans()  # type: ignore[operator]
        assert recorded.name == "sync.symbol"


class TestTheEnvironmentContract:
    def test_the_semconv_variable_is_set_before_the_instrumentors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is read ONCE, when the first instrumentor initialises. Set
        after that and the spans carry the old attribute names while every
        dashboard built on the stable ones is silently empty."""
        import inspect

        monkeypatch.delenv(tracing.SEMCONV_VAR, raising=False)
        source = inspect.getsource(tracing.configure_tracing)
        opt_in = source.index("SEMCONV_VAR")
        first_import = source.index("from opentelemetry")
        assert opt_in < first_import

    def test_an_operator_s_own_value_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(tracing.SEMCONV_VAR, "http")
        monkeypatch.setenv(tracing.ENDPOINT_VAR, "http://localhost:4317")
        tracing.configure_tracing("sync")
        import os

        assert os.environ[tracing.SEMCONV_VAR] == "http"

    def test_no_sampler_is_passed_to_the_provider(self) -> None:
        """A sampler built in code silently overrides `OTEL_TRACES_SAMPLER`
        and `OTEL_TRACES_SAMPLER_ARG`, so the compose override that sets
        0.1 for the API and 1.0 for the pipeline would be read and
        ignored. Leaving the argument off is the feature."""
        assert "sampler=" not in _code(tracing.configure_tracing)

    def test_psycopg_is_not_instrumented(self) -> None:
        """It would nest a second span under every SQLAlchemy span to say
        the same thing twice, doubling the volume for no new information."""
        assert "psycopg" not in _code(tracing.instrument_sqlalchemy)
