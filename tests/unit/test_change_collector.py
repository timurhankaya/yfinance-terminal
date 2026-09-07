"""What the collector records, and what it refuses to.

No database: `flush` is the only method that needs one, and what it hands
the COPY is a string these tests can read. Everything else here is the
decision of what becomes an event at all.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pytest

from yfin.storage.changes import ENVELOPE_VERSION, ChangeCollector, ChangeContext

TS = datetime(2026, 9, 7, 10, 15, 32, 118000, tzinfo=UTC)


def _collector(*, run_id: int | None = 4711, threshold: int = 1000) -> ChangeCollector:
    return ChangeCollector(ChangeContext(run_id=run_id, range_threshold=threshold))


def _envelopes(collector: ChangeCollector) -> list[dict[str, object]]:
    return [json.loads(event.payload) for event in collector.pending]


class TestRecording:
    def test_a_row_becomes_one_envelope(self) -> None:
        c = _collector()
        c.enter_dataset("income_statement")
        c.record(
            "financial_facts",
            "insert",
            {"symbol": "AAPL", "item_key": "TotalRevenue"},
            {"symbol": "AAPL", "item_key": "TotalRevenue", "value": Decimal("1.5")},
        )
        (envelope,) = _envelopes(c)
        assert envelope["v"] == ENVELOPE_VERSION
        assert envelope["op"] == "insert"
        assert envelope["family"] == "fundamentals"
        assert envelope["dataset"] == "income_statement"
        assert envelope["table"] == "financial_facts"
        assert envelope["run_id"] == 4711
        assert envelope["key"] == {"symbol": "AAPL", "item_key": "TotalRevenue"}

    def test_the_family_and_key_come_from_the_routing_map(self) -> None:
        """Not from the dataset: six datasets write `symbols`, and a
        per-dataset family would send one table's rows to two topics."""
        c = _collector()
        c.record("news", "insert", {"news_id": "abc"}, {"news_id": "abc"})
        (event,) = c.pending
        assert (event.family, event.partition_key) == ("news", "abc")

    def test_a_delete_carries_no_row(self) -> None:
        c = _collector()
        c.record("company_officers", "delete", {"symbol": "AAPL", "name": "Tim"}, None)
        (envelope,) = _envelopes(c)
        assert envelope["op"] == "delete"
        assert envelope["row"] is None

    def test_the_partition_key_is_read_from_the_key(self) -> None:
        """A delete has the key and nothing else, which is why every
        partition column is part of the primary key."""
        c = _collector()
        c.record("calendar_economic", "delete", {"region": "US", "event_id": "x"}, None)
        assert c.pending[0].partition_key == "US"

    def test_infrastructure_tables_are_ignored(self) -> None:
        """Publishing an event about queueing an event is the obvious
        infinite regress; the gate tables say 'we checked', not 'this moved'."""
        c = _collector()
        for table in ("sync_run_items", "asof_state", "pipeline_outbox", "bar_gaps"):
            c.record(table, "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        assert c.pending == []

    def test_an_unrouted_table_is_an_error(self) -> None:
        """Failing loudly beats dropping a table's changes in silence."""
        with pytest.raises(KeyError):
            _collector().record("not_a_table", "insert", {"symbol": "A"}, {})

    def test_the_dataset_is_null_outside_one(self) -> None:
        """`purge` and `bars rescale` run outside any dataset."""
        c = _collector()
        c.record("symbols", "delete", {"symbol": "AAPL"}, None)
        assert _envelopes(c)[0]["dataset"] is None

    def test_entering_a_dataset_relabels_only_what_follows(self) -> None:
        c = _collector()
        c.enter_dataset("news")
        c.record("news", "insert", {"news_id": "a"}, {"news_id": "a"})
        c.enter_dataset("search")
        c.record("news", "insert", {"news_id": "b"}, {"news_id": "b"})
        assert [e["dataset"] for e in _envelopes(c)] == ["news", "search"]

    def test_run_id_is_null_outside_a_sync(self) -> None:
        c = _collector(run_id=None)
        c.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        assert _envelopes(c)[0]["run_id"] is None


class TestRendering:
    """The envelope goes through `canonical_json`, so it inherits the
    handling the rest of the codebase already agreed on."""

    def test_a_decimal_is_text(self) -> None:
        """A float would reintroduce the f32 artefact this pipeline exists
        to remove."""
        c = _collector()
        c.record("price_bars", "insert", {"symbol": "A"}, {"close": Decimal("232.35")})
        assert _envelopes(c)[0]["row"] == {"close": "232.35"}

    def test_a_datetime_is_iso_8601(self) -> None:
        c = _collector()
        c.record("price_bars", "insert", {"symbol": "A"}, {"ts_utc": TS})
        assert _envelopes(c)[0]["row"] == {"ts_utc": "2026-09-07T10:15:32.118000+00:00"}

    def test_a_date_is_iso_8601(self) -> None:
        c = _collector()
        c.record("shares_full", "insert", {"symbol": "A"}, {"as_of_date": date(2026, 9, 7)})
        assert _envelopes(c)[0]["row"] == {"as_of_date": "2026-09-07"}

    def test_nan_becomes_null(self) -> None:
        """`allow_nan=False` would otherwise raise and lose the whole batch."""
        c = _collector()
        c.record("price_bars", "insert", {"symbol": "A"}, {"close": float("nan")})
        assert _envelopes(c)[0]["row"] == {"close": None}

    def test_a_numpy_scalar_is_unwrapped(self) -> None:
        c = _collector()
        c.record("price_bars", "insert", {"symbol": "A"}, {"volume": np.int64(42)})
        assert _envelopes(c)[0]["row"] == {"volume": 42}

    def test_a_tab_or_newline_cannot_break_the_copy_body(self) -> None:
        """The payload is one COPY field; an unescaped tab would shift every
        following column by one."""
        c = _collector()
        c.record("news", "insert", {"news_id": "a"}, {"news_id": "a", "title": "x\ty\nz"})
        body = c.copy_body(TS)
        assert body.count("\n") == 1  # the row terminator, not the payload
        assert body.count("\t") == 3  # four columns, three separators


class TestRangeCoalescing:
    def test_a_range_event_replaces_the_rows(self) -> None:
        """A first sync writes ~20k bars per symbol; across 4,500 symbols
        that is ~10^8 row events to say 'the history is here'."""
        c = _collector()
        c.record_range(
            "price_bars",
            "AAPL",
            kind="write",
            bar_interval="1m",
            ts_column="ts_utc",
            ts_from=TS,
            ts_to=TS,
            rows=20000,
        )
        (envelope,) = _envelopes(c)
        assert envelope["op"] == "range"
        assert envelope["key"] == {"symbol": "AAPL"}
        assert envelope["row"] == {
            "kind": "write",
            "bar_interval": "1m",
            "ts_column": "ts_utc",
            "ts_from": "2026-09-07T10:15:32.118000+00:00",
            "ts_to": "2026-09-07T10:15:32.118000+00:00",
            "rows": 20000,
        }

    def test_a_date_keyed_bar_table_has_no_interval(self) -> None:
        """`dividends` and friends are keyed by a date, so `ts_column` names
        which one and `bar_interval` is null."""
        c = _collector()
        c.record_range(
            "dividends",
            "AAPL",
            kind="write",
            bar_interval=None,
            ts_column="ex_date",
            ts_from=date(2020, 1, 1),
            ts_to=date(2026, 1, 1),
            rows=90,
        )
        row = _envelopes(c)[0]["row"]
        assert isinstance(row, dict)
        assert row["bar_interval"] is None
        assert row["ts_column"] == "ex_date"

    def test_a_purge_range_leaves_the_span_open(self) -> None:
        """Returning millions of bar keys from a DELETE is the cost the
        range event exists to avoid, so the span is not read back."""
        c = _collector()
        c.record_range(
            "price_bars",
            "AAPL",
            kind="delete",
            bar_interval=None,
            ts_column="ts_utc",
            ts_from=None,
            ts_to=None,
            rows=1_200_000,
        )
        row = _envelopes(c)[0]["row"]
        assert isinstance(row, dict)
        assert (row["kind"], row["ts_from"], row["ts_to"]) == ("delete", None, None)

    def test_a_range_on_a_non_bars_table_is_refused(self) -> None:
        """Coalescing exists because bar writes are enormous; using it
        elsewhere would hide changes a consumer can apply directly."""
        with pytest.raises(ValueError, match="bars"):
            _collector().record_range(
                "financial_facts",
                "AAPL",
                kind="write",
                bar_interval=None,
                ts_column="period_end",
                ts_from=None,
                ts_to=None,
                rows=5,
            )


class TestRescale:
    def test_a_rescale_names_the_split_not_the_rows(self) -> None:
        c = _collector()
        c.record_rescale("AAPL", date(2020, 8, 31), Decimal("4"), TS)
        (envelope,) = _envelopes(c)
        assert envelope["op"] == "rescale"
        assert envelope["table"] == "price_bars"
        assert envelope["dataset"] is None
        assert envelope["key"] == {"symbol": "AAPL", "split_date": "2020-08-31"}
        assert envelope["row"] == {
            "factor": "4",
            "applied_before": "2026-09-07T10:15:32.118000+00:00",
        }


class TestCopyBody:
    def test_one_line_per_event_in_column_order(self) -> None:
        c = _collector()
        c.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        c.record("symbols", "insert", {"symbol": "MSFT"}, {"symbol": "MSFT"})
        lines = c.copy_body(TS).splitlines()
        assert len(lines) == 2
        created_at, family, key, payload = lines[0].split("\t")
        assert created_at == str(TS)
        assert (family, key) == ("reference", "AAPL")
        assert json.loads(payload)["table"] == "symbols"

    def test_occurred_at_equals_the_flush_timestamp(self) -> None:
        """Both come from one `clock_timestamp()`, so the database clock
        orders events across shard processes and hosts."""
        c = _collector()
        c.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        body = c.copy_body(TS)
        payload = json.loads(body.split("\t")[3])
        assert payload["occurred_at"] == TS.isoformat()
        assert body.split("\t")[0] == str(TS)

    def test_an_empty_collector_renders_nothing(self) -> None:
        assert _collector().copy_body(TS) == ""
