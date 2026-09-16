"""Writer batching, symbol filtering and reject sampling: the parts that decide what reaches
the database. COPY encoding is in `test_storage_copy.py`; the database side in tests/repo."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from yfin.stream.connection import ConnectionHealth
from yfin.stream.rejects import REJECT_DECODE_FAILED, REJECT_UNKNOWN_SYMBOL, Reject
from yfin.stream.repository import ScopeEntry
from yfin.stream.supervisor import StreamSupervisor, SupervisorConfig
from yfin.stream.writer import (
    TICK_COLUMNS,
    RejectSampler,
    StreamWriter,
    TickWrite,
    WriterConfig,
)

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.counters: list[dict[str, int]] = []
        self.health: list[dict[str, Any]] = []

    def load_scope(self) -> list[ScopeEntry]:
        return []

    def count_symbols_missing_exchange(self) -> int:
        return 0

    def open_session(self, **_: Any) -> int:
        return 1

    def add_session_counters(self, session_id: int, **fields: int) -> None:
        self.counters.append(fields)

    def record_health(self, session_id: int, **fields: Any) -> None:
        self.health.append(fields)


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


# --- health ----------------------------------------------------------------


def test_health_is_written_by_this_thread_not_the_event_loop() -> None:
    """The supervisor boxes it; this is where it reaches the database."""
    writer, supervisor, repository = _writer()
    supervisor._on_health(ConnectionHealth(connection_key="NMS", state="open", subscribed_count=4))
    writer._flush_health()
    assert repository.health == [
        {
            "connection_key": "NMS",
            "state": "open",
            "subscribed_count": 4,
            "connected_at": None,
            "last_message_at": None,
            "last_canary_at": None,
            "reconnect_count": 0,
            "last_error": None,
        }
    ]


def test_health_is_written_once_per_cycle_not_once_per_canary() -> None:
    """The canary ticks on every connection around the clock; the row it
    produces is one INSERT per writer batch, not one per message."""
    writer, supervisor, repository = _writer()
    for _ in range(50):
        supervisor._on_health(ConnectionHealth(connection_key="NMS", state="open"))
    writer._flush_health()
    assert len(repository.health) == 1
    writer._flush_health()
    assert len(repository.health) == 1


def test_no_session_leaves_health_in_the_box() -> None:
    """Connections emit state while the session row is still being opened.
    The box keeps only the newest per connection, so waiting costs
    nothing and loses nothing."""
    writer, supervisor, repository = _writer()
    writer._session_id = None
    supervisor._on_health(ConnectionHealth(connection_key="NMS", state="open"))
    writer._flush_health()
    assert repository.health == []
    writer.set_session(1)
    writer._flush_health()
    assert len(repository.health) == 1


def test_a_quiet_cycle_still_writes_health() -> None:
    """A connection that has gone quiet is exactly when its row matters."""
    writer, supervisor, repository = _writer(batch_interval_ms=1)
    supervisor._on_health(ConnectionHealth(connection_key="NMS", state="open"))
    writer._cycle()
    assert len(repository.health) == 1
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
    """The batch-level half of the rollback guard: the database guard only sees the row
    dedupe hands it, and plain last-wins would hand it the older tick."""
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


# --- the browser fan-out ----------------------------------------------------
#
# Ordering, not delivery: `publish.py` owns what a tick body looks like and
# what happens when Redis is down (test_stream_publish.py). What is decided
# HERE is that nothing is published until the archive has the row.


class _RecordingPublisher:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def publish(self, rows: Any) -> None:
        self.batches.append(list(rows))

    def close(self) -> None:
        pass


class _FakeSession:
    def __init__(self, *, commit_fails: bool = False) -> None:
        self._commit_fails = commit_fails
        self.committed = False

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def commit(self) -> None:
        if self._commit_fails:
            raise RuntimeError("the transaction was rolled back")
        self.committed = True


def _publishing_writer(*, commit_fails: bool = False) -> tuple[StreamWriter, _RecordingPublisher]:
    """A writer whose database side is stubbed out: everything below `_write` needs a real
    PostgreSQL and is covered in tests/repo."""
    session = _FakeSession(commit_fails=commit_fails)
    writer, _, _ = _writer()
    writer._session_factory = lambda: session  # type: ignore[assignment,method-assign]
    writer._write_ticks = lambda _s, rows: TickWrite(  # type: ignore[method-assign]
        len(rows), [], list(rows)
    )
    writer._write_rejects = lambda _s, _r: None  # type: ignore[assignment,method-assign]
    writer._write_quotes = lambda _s: None  # type: ignore[assignment,method-assign]
    writer._flush_counters = lambda **_k: None  # type: ignore[assignment,method-assign]
    publisher = _RecordingPublisher()
    writer._publisher = publisher  # type: ignore[assignment]
    return writer, publisher


def test_accepted_rows_are_published_after_the_commit() -> None:
    writer, publisher = _publishing_writer()
    rows = [_row(), _row(symbol="MSFT")]
    writer._write(rows, [])
    assert publisher.batches == [rows]


def test_a_failed_commit_publishes_nothing() -> None:
    """A tick on the page that the archive does not have is a price that,
    once the transaction rolls back, it never will."""
    writer, publisher = _publishing_writer(commit_fails=True)
    with pytest.raises(RuntimeError):
        writer._write([_row()], [])
    assert publisher.batches == []


def test_rows_the_foreign_key_refused_are_not_published() -> None:
    """`accepted` is the publish list, and it is what passed the filter."""
    writer, publisher = _publishing_writer()
    writer._write_ticks = lambda _s, _rows: TickWrite(  # type: ignore[method-assign]
        0, [Reject(reason=REJECT_UNKNOWN_SYMBOL, symbol="NOPE", detail="no row")], []
    )
    writer._write([_row(symbol="NOPE")], [])
    assert publisher.batches == [[]]
