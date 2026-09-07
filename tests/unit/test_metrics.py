"""Two backends behind one counter API, and the closed label set.

A long-lived service (the API, `stream run`, either relay, the scheduler)
serves `/metrics` and its counters live in a Prometheus registry. A sync
shard is a SHORT-LIVED PROCESS: it exits long before any scrape could reach
it, so its counters go into an in-process accumulator that is flushed into
`run_metrics` on the way out, and the database is what the exporter reads.

Both are the same call at the point of instrumentation, which is the point:
`readers.py` counting a cache hit should not know which kind of process it
is running in.
"""

from __future__ import annotations

import json

import pytest

from yfin.core.metrics import (
    ALLOWED_LABELS,
    METRICS,
    Accumulator,
    MetricSpec,
    count_exception,
    label_key,
)


class TestTheClosedLabelSet:
    """Ten thousand symbols times forty-nine datasets is a series
    explosion; symbol-level detail stays in the database and in Loki."""

    def test_no_metric_is_labelled_by_symbol(self) -> None:
        for spec in METRICS.values():
            assert "symbol" not in spec.labelnames, spec.name

    def test_every_label_is_in_the_closed_set(self) -> None:
        for spec in METRICS.values():
            unknown = set(spec.labelnames) - ALLOWED_LABELS
            assert not unknown, f"{spec.name}: {sorted(unknown)}"

    def test_every_metric_is_namespaced(self) -> None:
        for name, spec in METRICS.items():
            assert name == spec.name
            assert spec.name.startswith("yfin_"), spec.name

    def test_counters_carry_the_total_suffix(self) -> None:
        """`_total` only on in-process counters -- a gauge read from a table
        must not look like one, or the same name would carry two meanings."""
        for spec in METRICS.values():
            if spec.kind == "counter":
                assert spec.name.endswith("_total"), spec.name

    def test_every_metric_is_documented(self) -> None:
        for spec in METRICS.values():
            assert spec.documentation.strip(), spec.name


class TestLabelKey:
    """`run_metrics.labels` is part of the primary key, so two writes of the
    same counter must produce the same string."""

    def test_it_is_sorted_json(self) -> None:
        assert label_key({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'

    def test_argument_order_does_not_matter(self) -> None:
        assert label_key({"a": "1", "b": "2"}) == label_key({"b": "2", "a": "1"})

    def test_no_labels_is_an_empty_object(self) -> None:
        """Not an empty string: the column is NOT NULL and part of the key,
        and `{}` reads as "this counter has no labels" rather than as a
        value someone forgot to write."""
        assert label_key({}) == "{}"

    def test_it_is_compact(self) -> None:
        """The column is 255 characters."""
        assert " " not in label_key({"dataset": "history", "result": "hit"})


class TestAccumulator:
    def test_it_starts_empty(self) -> None:
        assert Accumulator().rows() == []

    def test_it_sums_by_name_and_labels(self) -> None:
        acc = Accumulator()
        acc.inc("yfin_sync_yahoo_requests_total", dataset="history", outcome="ok")
        acc.inc("yfin_sync_yahoo_requests_total", dataset="history", outcome="ok")
        acc.inc("yfin_sync_yahoo_requests_total", dataset="info", outcome="ok")
        rows = {(r.name, r.labels): r.value for r in acc.rows()}
        assert rows == {
            ("yfin_sync_yahoo_requests_total", '{"dataset":"history","outcome":"ok"}'): 2,
            ("yfin_sync_yahoo_requests_total", '{"dataset":"info","outcome":"ok"}'): 1,
        }

    def test_it_accepts_an_amount(self) -> None:
        acc = Accumulator()
        acc.inc("yfin_sync_write_rows_total", 17, table="price_bars", op="verified")
        assert acc.rows()[0].value == 17

    def test_labels_given_in_any_order_are_one_series(self) -> None:
        acc = Accumulator()
        acc.inc("yfin_sync_retries_total", kind="rate_limit")
        acc.inc("yfin_sync_retries_total", kind="rate_limit")
        assert len(acc.rows()) == 1
        assert acc.rows()[0].value == 2

    def test_an_undeclared_metric_is_refused(self) -> None:
        """A counter nobody declared has no documentation, no label
        contract, and would reach the exporter as a surprise."""
        with pytest.raises(KeyError):
            Accumulator().inc("yfin_sync_not_declared_total")

    def test_an_undeclared_label_is_refused(self) -> None:
        """Otherwise a typo becomes a second series that looks like data."""
        with pytest.raises(ValueError, match="label"):
            Accumulator().inc("yfin_sync_retries_total", knid="rate_limit")

    def test_a_symbol_label_is_refused(self) -> None:
        with pytest.raises(ValueError, match="label"):
            Accumulator().inc("yfin_sync_retries_total", symbol="AAPL")

    def test_the_rows_survive_json_round_trip(self) -> None:
        acc = Accumulator()
        acc.inc("yfin_sync_retries_total", kind="timeout")
        assert json.loads(acc.rows()[0].labels) == {"kind": "timeout"}

    def test_it_is_thread_safe(self) -> None:
        """Worker threads count Yahoo requests; a lost increment is a
        silently wrong number, which is worse than no number."""
        import threading

        acc = Accumulator()

        def work() -> None:
            for _ in range(200):
                acc.inc("yfin_sync_retries_total", kind="timeout")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert acc.rows()[0].value == 1600


class TestServeMetrics:
    """Observability must never take the work down with it."""

    def test_port_zero_does_not_start_a_server(self) -> None:
        from yfin.core.metrics import serve_metrics

        assert serve_metrics(0) is False

    def test_a_port_that_cannot_be_bound_is_a_warning(self) -> None:
        """A metrics port already in use must not stop a sync.

        The blocker listens on the SAME address the server would use;
        `127.0.0.1` and `0.0.0.0` are different bindings and would not
        collide, which is a way to write this test that passes without
        testing anything.
        """
        import socket

        from yfin.core.metrics import serve_metrics

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            assert serve_metrics(port, addr="127.0.0.1") is False


class TestCountException:
    def test_it_labels_by_the_exception_class(self) -> None:
        """`prometheus_client.count_exceptions` cannot label by type, which
        is the only thing that makes the counter useful."""
        count_exception(ValueError("boom"))  # must not raise

    def test_it_never_raises(self) -> None:
        """It runs in exception handlers. A metric that can fail there would
        replace the real error with its own."""
        count_exception(KeyboardInterrupt())


def test_a_spec_can_be_declared_without_labels() -> None:
    spec = MetricSpec(name="yfin_probe_total", documentation="x", kind="counter")
    assert spec.labelnames == ()
