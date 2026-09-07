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


class TestGauges:
    """The exporter's side of the API.

    A gauge is a NUMBER READ FROM A TABLE, republished on the scheduler's
    `/metrics`. It has no `_total`, it can go down, and the set of label
    combinations it carries changes between refreshes -- a dataset can leave
    the universe, a proxy can be deleted, a stream connection can close.
    """

    def test_gauges_do_not_carry_the_total_suffix(self) -> None:
        """`yfin_cells_total` is the one exception, and it is the spec's own
        name: there `_total` is the denominator of `yfin_cells_stale`, not
        the Prometheus counter suffix. Everything else read from a table
        drops it, so a gauge never looks like a monotonic counter."""
        for spec in METRICS.values():
            if spec.kind == "gauge" and spec.name != "yfin_cells_total":
                assert not spec.name.endswith("_total"), spec.name

    def test_no_name_is_both_a_counter_and_a_gauge(self) -> None:
        """The whole reason the exported names drop `_total`: one name with
        two label sets cannot be registered, and would not mean one thing.

        Comparing `{spec.name}` against `len(METRICS)` cannot fail: METRICS
        is five dicts merged and every one is keyed by `spec.name`, so a
        collision does not show up as a duplicate -- it silently OVERWRITES,
        and the count matches either way. Counting the sources instead is
        what makes the collision visible.
        """
        from yfin.core.metrics import (
            _EXPORTER_GAUGES,
            _PROCESS_METRICS,
            _SERVICE_COUNTERS,
            _SHARD_COUNTERS,
            _republished,
        )

        sources = (
            _PROCESS_METRICS,
            _SERVICE_COUNTERS,
            _SHARD_COUNTERS,
            _republished(_SHARD_COUNTERS),
            _EXPORTER_GAUGES,
        )
        assert sum(len(d) for d in sources) == len(METRICS)

    def test_an_undeclared_label_is_refused(self) -> None:
        """Same contract as the counters: `set_gauge` swallows it, but the
        validation underneath is what a test can see."""
        from yfin.core.metrics import _validate

        with pytest.raises(ValueError, match="label"):
            _validate("yfin_cells_total", {"symbol": "AAPL"})
        assert "scope" in METRICS["yfin_cells_total"].labelnames

    def test_setting_a_gauge_never_raises(self) -> None:
        """An exporter that crashed on a bad label would take the scheduler
        process with it, and the scheduler is what runs the jobs."""
        from yfin.core.metrics import set_gauge

        set_gauge("yfin_not_declared_at_all", 1)
        set_gauge("yfin_cells_total", 1, nonsense="x")

    def test_clearing_one_never_raises(self) -> None:
        from yfin.core.metrics import clear_gauge

        clear_gauge("yfin_not_declared_at_all")

    def test_a_vanished_label_set_can_be_dropped(self) -> None:
        """A cell that leaves the universe must stop being reported, or the
        last value it had would sit on the dashboard forever."""
        from yfin.core.metrics import _object, clear_gauge, set_gauge

        def series() -> list[tuple[str, ...]]:
            """This gauge's label combinations, and no other metric's.

            Read off the object rather than out of `generate_latest()`: the
            whole registry is process-wide, so any other test that ever
            published a `market_status` label would make a text search pass
            or fail for reasons that have nothing to do with clearing.
            """
            return [
                tuple(sample.labels.values())
                for metric in _object("yfin_cells_total").collect()
                for sample in metric.samples
            ]

        set_gauge("yfin_cells_total", 7, scope="market", dataset="market_status")
        assert ("market", "market_status") in series()
        clear_gauge("yfin_cells_total")
        assert series() == []


class TestTheRepublishedSyncCounters:
    """What a shard accumulated, read back out of `run_metrics`.

    The exporter publishes them as GAUGES with an extra `scope` label, and
    without `_total`: the value is the latest run's, not a monotonic total
    of this process's, and a name must never carry two label sets.
    """

    def test_every_shard_counter_has_an_exported_gauge(self) -> None:
        from yfin.core.metrics import exported_name

        for spec in METRICS.values():
            if spec.name.startswith("yfin_sync_") and spec.kind == "counter":
                gauge = METRICS[exported_name(spec.name)]
                assert gauge.kind == "gauge"
                assert gauge.labelnames == ("scope", *spec.labelnames)

    def test_the_exported_name_drops_the_total(self) -> None:
        from yfin.core.metrics import exported_name

        assert exported_name("yfin_sync_retries_total") == "yfin_sync_retries"


class TestTheServiceCounters:
    """What the long-lived services count, and why the names differ from
    the exporter's gauges for the same thing."""

    def test_observing_never_raises(self) -> None:
        """A batch that failed to be timed is still a batch that was
        written, and the timing is worth less than the write."""
        from yfin.core.metrics import observe

        observe("yfin_not_declared", 1.0)
        observe("yfin_stream_batch_seconds", 1.0, nonsense="x")

    def test_timed_records_even_when_the_block_raises(self) -> None:
        """A pass that failed is still a pass that took time; dropping it
        would flatten the histogram exactly when something is going wrong."""
        from prometheus_client import generate_latest

        from yfin.core.metrics import timed

        with pytest.raises(ValueError, match="boom"), timed(
            "yfin_relay_pass_seconds", outbox="stream_outbox"
        ):
            raise ValueError("boom")
        assert b"yfin_relay_pass_seconds_count" in generate_latest()

    def test_the_stream_counter_and_the_stream_gauge_are_different_names(
        self,
    ) -> None:
        """The table says what the CURRENT session has seen; the counter
        says what this process has seen since it started. A reconnect storm
        that ends in a new session shows in one and not the other."""
        assert METRICS["yfin_stream_reconnects_total"].kind == "counter"
        assert METRICS["yfin_stream_reconnects"].kind == "gauge"

    def test_every_relay_metric_is_labelled_by_outbox(self) -> None:
        """Two relays run the same code in two processes. Without the
        label a tick backlog and a change backlog would be one number."""
        for name, spec in METRICS.items():
            if name.startswith("yfin_relay_"):
                assert "outbox" in spec.labelnames, name

    def test_the_histograms_are_declared_as_histograms(self) -> None:
        for name in ("yfin_stream_batch_seconds", "yfin_relay_pass_seconds"):
            assert METRICS[name].kind == "histogram"


class TestTheReservedLabels:
    """`job` and `instance` belong to Prometheus, not to us.

    A scrape stamps both from the scrape config. A metric that carries its
    own is not rejected -- it is silently RENAMED to `exported_job` while
    `job` becomes the scrape job's name, so every `by (job)` in a dashboard
    groups by a label with one value and every alert that filters on it
    matches nothing.

    Found on the running stack, not in review: the series looked right in
    the exposition text and wrong only after Prometheus had ingested it.
    """

    def test_no_metric_uses_a_reserved_label(self) -> None:
        for spec in METRICS.values():
            assert "job" not in spec.labelnames, spec.name
            assert "instance" not in spec.labelnames, spec.name

    def test_the_closed_set_does_not_offer_them(self) -> None:
        """Removed from ALLOWED_LABELS too, so the next metric that wants
        to name a job cannot reach for `job` and pass validation."""
        assert "job" not in ALLOWED_LABELS
        assert "instance" not in ALLOWED_LABELS
        assert "job_name" in ALLOWED_LABELS

    def test_the_scheduler_metrics_use_job_name(self) -> None:
        for name, spec in METRICS.items():
            if name.startswith("yfin_job_"):
                assert "job_name" in spec.labelnames, name
