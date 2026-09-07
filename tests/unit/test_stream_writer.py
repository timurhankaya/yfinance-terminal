"""Writer batching, COPY encoding, symbol filtering and reject sampling.

The database side lives in tests/repo; these cover the parts that decide
what reaches it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from yfin.stream.protocol import REJECT_DECODE_FAILED, REJECT_UNKNOWN_SYMBOL, Reject
from yfin.stream.repository import ScopeEntry
from yfin.stream.supervisor import StreamSupervisor, SupervisorConfig
from yfin.stream.writer import (
    TICK_COLUMNS,
    RejectSampler,
    StreamWriter,
    WriterConfig,
    copy_body,
)

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.counters: list[dict[str, int]] = []

    def load_scope(self) -> list[ScopeEntry]:
        return []

    def count_symbols_missing_exchange(self) -> int:
        return 0

    def open_session(self, **_: Any) -> int:
        return 1

    def add_session_counters(self, session_id: int, **fields: int) -> None:
        self.counters.append(fields)


def _row(symbol: str = "AAPL", ts: datetime = TS, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(TICK_COLUMNS)
    row.update(
        {
            "symbol": symbol,
            "ts_utc": ts,
            "payload_hash": "0" * 16,
            "received_at": ts,
            "quote_type_code": 8,
            "market_hours_code": 1,
            "price": Decimal("232.35"),
        }
    )
    row.update(extra)
    return row


def _writer(**config: Any) -> tuple[StreamWriter, StreamSupervisor, FakeRepository]:
    repository = FakeRepository()
    supervisor = StreamSupervisor(repository, config=SupervisorConfig())  # type: ignore[arg-type]
    writer = StreamWriter(
        supervisor,
        repository,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        config=WriterConfig(**config),
        session_id=1,
    )
    return writer, supervisor, repository


# --- COPY encoding ---------------------------------------------------------


def test_copy_body_writes_one_line_per_row() -> None:
    body = copy_body([_row("AAPL"), _row("MSFT")])
    assert body.count("\n") == 2


def test_copy_body_uses_the_schema_column_order() -> None:
    """Taken from the model, so it cannot drift from the table."""
    body = copy_body([_row()], columns=("symbol", "ts_utc"))
    assert body.startswith("AAPL\t2026-09-07 14:30:00+00:00")


def test_copy_body_encodes_null() -> None:
    body = copy_body([_row(bid=None)], columns=("symbol", "bid"))
    assert body == "AAPL\t\\N\n"


def test_copy_body_keeps_decimal_precision() -> None:
    """The whole point of f32_decimal would be lost to a float repr here."""
    body = copy_body([_row(price=Decimal("232.35"))], columns=("price",))
    assert body == "232.35\n"


def test_copy_body_escapes_structural_characters() -> None:
    """`unknown_fields` is upstream JSON: a tab or newline in it would
    otherwise shift every following column by one."""
    body = copy_body(
        [_row(unknown_fields='{"a":"x\ty"}')], columns=("symbol", "unknown_fields")
    )
    assert body == 'AAPL\t{"a":"x\\ty"}\n'
    assert body.count("\t") == 1  # the separator, not the payload


def test_copy_body_escapes_backslashes() -> None:
    body = copy_body([_row(unknown_fields="a\\b")], columns=("unknown_fields",))
    assert body == "a\\\\b\n"


def test_tick_columns_match_the_table() -> None:
    from yfin.models.stream import LiveTick

    assert set(TICK_COLUMNS) == set(LiveTick.__table__.c.keys())


# --- batching --------------------------------------------------------------


def test_batch_stops_at_the_size_limit() -> None:
    writer, supervisor, _ = _writer(batch_size=3, batch_interval_ms=500)
    for i in range(10):
        supervisor.queue.put(_row(ts=TS + timedelta(seconds=i)))
    assert len(writer._collect()) == 3


def test_batch_stops_at_the_time_limit() -> None:
    """Without this the last ticks of a quiet market would wait minutes."""
    writer, supervisor, _ = _writer(batch_size=1000, batch_interval_ms=30)
    supervisor.queue.put(_row())
    started = time.monotonic()
    rows = writer._collect()
    assert len(rows) == 1
    assert time.monotonic() - started < 1.0


def test_empty_queue_returns_an_empty_batch() -> None:
    writer, _, _ = _writer(batch_interval_ms=10)
    assert writer._collect() == []


def test_rejects_are_collected_without_blocking() -> None:
    writer, supervisor, _ = _writer()
    supervisor.rejects.put(Reject(REJECT_DECODE_FAILED))
    assert len(writer._collect_rejects()) == 1
    assert writer._collect_rejects() == []


# --- reject sampling -------------------------------------------------------


def test_sampler_caps_a_repeated_pair() -> None:
    """One broken feed must not fill the table."""
    sampler = RejectSampler(per_hour=3)
    reject = Reject(REJECT_UNKNOWN_SYMBOL, symbol="NOPE")
    allowed = [sampler.allow(reject) for _ in range(10)]
    assert allowed.count(True) == 3


def test_sampler_counts_pairs_separately() -> None:
    sampler = RejectSampler(per_hour=1)
    assert sampler.allow(Reject(REJECT_UNKNOWN_SYMBOL, symbol="A")) is True
    assert sampler.allow(Reject(REJECT_UNKNOWN_SYMBOL, symbol="B")) is True
    assert sampler.allow(Reject(REJECT_DECODE_FAILED, symbol="A")) is True
    assert sampler.allow(Reject(REJECT_UNKNOWN_SYMBOL, symbol="A")) is False


def test_sampler_reopens_after_an_hour() -> None:
    sampler = RejectSampler(per_hour=1)
    reject = Reject(REJECT_UNKNOWN_SYMBOL, symbol="A")
    assert sampler.allow(reject) is True
    assert sampler.allow(reject) is False
    # Rewind the window rather than sleeping an hour.
    key = (reject.symbol, reject.reason)
    start, count = sampler._seen[key]
    sampler._seen[key] = (start - timedelta(hours=2), count)
    assert sampler.allow(reject) is True


# --- counters --------------------------------------------------------------


def test_counters_are_flushed_additively() -> None:
    writer, supervisor, repository = _writer()
    supervisor.counters.messages = 7
    supervisor.counters.dropped = 2
    writer._flush_counters(written=5)
    assert repository.counters == [
        {"messages": 7, "written": 5, "rejected": 0, "dropped": 2}
    ]


def test_nothing_to_flush_writes_nothing() -> None:
    """A quiet cycle should not produce an UPDATE per interval."""
    writer, _, repository = _writer()
    writer._flush_counters()
    assert repository.counters == []


def test_flushing_resets_the_supervisor_counters() -> None:
    """Otherwise every flush would re-add the same totals."""
    writer, supervisor, repository = _writer()
    supervisor.counters.messages = 3
    writer._flush_counters(written=1)
    writer._flush_counters(written=1)
    assert repository.counters[0]["messages"] == 3
    assert repository.counters[1]["messages"] == 0


def test_no_session_means_no_counter_write() -> None:
    writer, supervisor, repository = _writer()
    writer._session_id = None
    supervisor.counters.messages = 5
    writer._flush_counters(written=1)
    assert repository.counters == []


# --- failure handling ------------------------------------------------------


def test_a_dying_writer_records_the_failure() -> None:
    """The runner checks this: a dead writer means the queue fills and the
    supervisor drops every tick while the process still looks healthy."""
    writer, supervisor, _ = _writer()

    def explode() -> None:
        raise RuntimeError("connection gone")

    writer._cycle = explode  # type: ignore[method-assign]
    writer.run()
    assert isinstance(writer.failed, RuntimeError)


def test_stop_lets_run_finish() -> None:
    writer, _, _ = _writer(batch_interval_ms=10)
    writer.stop()
    writer.run()
    assert writer.failed is None


# --- the guard, in the batch ------------------------------------------------


def test_dedupe_keeps_the_newest_row_whole() -> None:
    """The batch-level half of the rollback guard.

    The database guard only ever sees the row dedupe hands it. Plain
    last-wins would hand it the older tick and the guard would then
    correctly refuse to apply... the wrong row.
    """
    from yfin.storage.persistence import dedupe_rows

    newer = {"symbol": "AAPL", "ts_utc": TS, "price": Decimal("10")}
    older = {"symbol": "AAPL", "ts_utc": TS - timedelta(minutes=5), "price": Decimal("9")}
    kept = dedupe_rows([newer, older], ("symbol",), (), "ts_utc")
    assert len(kept) == 1
    assert kept[0]["ts_utc"] == TS
    assert kept[0]["price"] == Decimal("10")


def test_dedupe_takes_the_newer_row_when_it_arrives_second() -> None:
    from yfin.storage.persistence import dedupe_rows

    older = {"symbol": "AAPL", "ts_utc": TS - timedelta(minutes=5), "price": Decimal("9")}
    newer = {"symbol": "AAPL", "ts_utc": TS, "price": Decimal("10")}
    kept = dedupe_rows([older, newer], ("symbol",), (), "ts_utc")
    assert kept[0]["price"] == Decimal("10")


def test_dedupe_does_not_merge_across_instants() -> None:
    """A row must never mix fields from two different ticks.

    Column-wise merging would produce a quote that never existed on any
    exchange -- one instant's price beside another's bid.
    """
    from yfin.storage.persistence import dedupe_rows

    newer = {"symbol": "AAPL", "ts_utc": TS, "price": Decimal("10"), "bid": None}
    older = {
        "symbol": "AAPL",
        "ts_utc": TS - timedelta(minutes=5),
        "price": Decimal("9"),
        "bid": Decimal("8"),
    }
    kept = dedupe_rows([newer, older], ("symbol",), (), "ts_utc")
    assert kept[0]["bid"] is None  # not backfilled from the older row


def test_dedupe_without_a_guard_is_unchanged() -> None:
    """The existing 57 call sites must behave exactly as before."""
    from yfin.storage.persistence import dedupe_rows

    first = {"symbol": "AAPL", "value": 1}
    second = {"symbol": "AAPL", "value": 2}
    assert dedupe_rows([first, second], ("symbol",), ())[0]["value"] == 2
