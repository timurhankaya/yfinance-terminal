"""The tick wire shape and the fan-out that is allowed to fail.

Two things are covered here and nowhere else: that a tick body says the
same thing whether it came from the writer's row dict or from a
`live_quotes` row (a `snap` that disagreed with the ticks after it draws
a jump on every chart at subscribe time), and that a Redis that is down
costs a counter and one log line rather than the archive.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from yfin.core.metrics import Accumulator, use_accumulator
from yfin.stream.publish import (
    TICK_FIELDS,
    TickPublisher,
    channel,
    tick_body,
)

TS = datetime(2026, 9, 8, 14, 30, 15, 250_000, tzinfo=UTC)
#: 2026-09-08T14:30:15.250Z
TS_MS = 1788877815250

REPO_ROOT = Path(__file__).resolve().parents[2]


def _row(**extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "AAPL",
        "ts_utc": TS,
        "price": Decimal("232.35"),
        "market_hours_code": 1,
    }
    row.update(extra)
    return row


class _Quote:
    """A `live_quotes` row, as SQLAlchemy hands one back."""

    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class FakePipeline:
    def __init__(self, owner: FakeRedis) -> None:
        self._owner = owner
        self.staged: list[tuple[str, str]] = []

    def publish(self, name: str, payload: str) -> None:
        self.staged.append((name, payload))

    def execute(self) -> None:
        if self._owner.fail:
            raise ConnectionError("redis is down")
        self._owner.published.extend(self.staged)


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, str]] = []
        self.closed = 0

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def counters() -> Any:
    """Counts into an in-memory accumulator rather than the Prometheus
    registry, which a unit test cannot read back without a scrape."""
    accumulator = Accumulator()
    use_accumulator(accumulator)
    yield accumulator
    use_accumulator(None)


def _events(logs: list[dict[str, Any]], fragment: str) -> int:
    """How many captured log events carried `fragment`.

    `structlog.testing.capture_logs` rather than `capsys` or `caplog`:
    the chain renders to the stream it was configured with, and by the
    time the whole suite has run that is not the one pytest is capturing.
    This intercepts before any renderer, so it holds whatever the process
    is configured for.
    """
    return sum(fragment in str(entry.get("event", "")) for entry in logs)


def _counts(accumulator: Accumulator) -> dict[str, int]:
    """`{result: count}` for the publish counter, by its only label."""
    return {
        json.loads(row.labels)["result"]: row.value
        for row in accumulator.rows()
        if row.name == "yfin_stream_publish_total"
    }


def _publisher(client: FakeRedis | None, **kwargs: Any) -> TickPublisher:
    publisher = TickPublisher(**{"enabled": True, "url": "redis://x", **kwargs})
    if client is not None:
        publisher._client = client  # type: ignore[assignment]
    return publisher


class TestTickBody:
    def test_the_four_required_fields_are_always_there(self) -> None:
        assert tick_body(_row()) == {"s": "AAPL", "t": TS_MS, "p": "232.35", "mh": 1}

    def test_empty_optional_fields_are_not_sent(self) -> None:
        """A tick body goes out per tick per subscriber; a null costs as
        much on the wire as a value."""
        body = tick_body(_row(bid=None, day_volume=None))
        assert "b" not in body and "v" not in body  # type: ignore[operator]

    def test_optional_fields_keep_their_short_keys(self) -> None:
        body = tick_body(_row(previous_close=Decimal("230.10"), day_volume=41_235_900))
        assert body is not None
        assert body["pc"] == "230.1"
        assert body["v"] == 41_235_900

    def test_decimals_stay_strings(self) -> None:
        """NUMERIC(28,12) does not survive a round trip through the
        browser's binary64, so it never becomes a JSON number."""
        body = tick_body(_row(price=Decimal("0.000000000001")))
        assert body is not None
        assert body["p"] == "0.000000000001"

    def test_trailing_zeros_from_the_column_scale_are_dropped(self) -> None:
        """`price` is NUMERIC(28,12), so a value read back from
        `live_quotes` is `232.500000000000`. Those twelve zeros say
        nothing and would be sent on every tick and shown in the
        time-and-sales list."""
        body = tick_body(_row(price=Decimal("232.500000000000")))
        assert body is not None
        assert body["p"] == "232.5"

    def test_a_whole_number_does_not_become_an_exponent(self) -> None:
        """`Decimal.normalize()` renders 100 as `1E+2` on its own."""
        body = tick_body(_row(price=Decimal("100.000")))
        assert body is not None
        assert body["p"] == "100"

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Not as the writer host's local time: every timestamp in this
        codebase is UTC, and a shifted one is a silent error that only
        appears off UTC."""
        assert tick_body(_row(ts_utc=TS.replace(tzinfo=None))) == tick_body(_row())

    def test_a_tick_with_no_price_is_not_publishable(self) -> None:
        assert tick_body(_row(price=None)) is None

    def test_a_quote_row_and_a_writer_row_agree(self) -> None:
        """The `snap` and the ticks after it come from the same table."""
        fields = _row(change=Decimal("1.25"), ask_size=100)
        assert tick_body(_Quote(**fields)) == tick_body(fields)

    def test_the_channel_is_per_symbol(self) -> None:
        assert channel("BRK-B") == "yfin:tick:BRK-B"


class TestPublisher:
    def test_off_when_the_switch_is_off(self, counters: Accumulator) -> None:
        publisher = _publisher(None, enabled=False)
        assert publisher.enabled is False
        publisher.publish([_row()])
        assert _counts(counters) == {"disabled": 1}

    def test_off_when_there_is_no_url(self, counters: Accumulator) -> None:
        """The switch is DB-managed and the URL is env-only, so "on with
        nowhere to publish to" is a configuration a deployment can reach."""
        assert TickPublisher(enabled=True, url="").enabled is False

    def test_an_empty_batch_is_not_counted(self, counters: Accumulator) -> None:
        _publisher(FakeRedis()).publish([])
        assert _counts(counters) == {}

    def test_one_publish_per_tick_on_its_own_channel(self, counters: Accumulator) -> None:
        client = FakeRedis()
        _publisher(client).publish([_row(), _row(symbol="MSFT", price=Decimal("501.10"))])
        assert [name for name, _ in client.published] == ["yfin:tick:AAPL", "yfin:tick:MSFT"]
        assert json.loads(client.published[0][1]) == {
            "s": "AAPL", "t": TS_MS, "p": "232.35", "mh": 1
        }
        assert _counts(counters) == {"ok": 1}

    def test_an_unpublishable_tick_does_not_stop_the_batch(self, counters: Accumulator) -> None:
        client = FakeRedis()
        _publisher(client).publish([_row(price=None), _row(symbol="MSFT")])
        assert [name for name, _ in client.published] == ["yfin:tick:MSFT"]

    def test_a_failure_is_counted_and_swallowed(self, counters: Accumulator) -> None:
        """The commit already happened. Raising here would turn a browser's
        missing price into a dead writer thread."""
        publisher = _publisher(FakeRedis(fail=True))
        publisher.publish([_row()])
        assert _counts(counters) == {"failed": 1}

    def test_a_failure_drops_the_client_so_the_next_batch_redials(
        self, counters: Accumulator
    ) -> None:
        publisher = _publisher(FakeRedis(fail=True))
        publisher.publish([_row()])
        assert publisher._client is None

    def test_the_warning_is_logged_once_per_outage(self, counters: Accumulator) -> None:
        """Redis down for an hour is 14,000 batches. A warning each would
        bury the line saying the stream itself is fine."""
        publisher = _publisher(None)
        with capture_logs() as logs:
            for _ in range(3):
                publisher._client = FakeRedis(fail=True)  # type: ignore[assignment]
                publisher.publish([_row()])
        assert _events(logs, "publish failing") == 1
        assert _counts(counters) == {"failed": 3}

    def test_recovery_is_logged_and_arms_the_next_warning(
        self, counters: Accumulator
    ) -> None:
        publisher = _publisher(FakeRedis(fail=True))
        with capture_logs() as logs:
            publisher.publish([_row()])
            publisher._client = FakeRedis()  # type: ignore[assignment]
            publisher.publish([_row()])
            publisher._client = FakeRedis(fail=True)  # type: ignore[assignment]
            publisher.publish([_row()])
        assert _events(logs, "publish failing") == 2
        assert _events(logs, "publish recovered") == 1
        assert _counts(counters) == {"failed": 2, "ok": 1}

    def test_close_is_safe_twice(self) -> None:
        client = FakeRedis()
        publisher = _publisher(client)
        publisher.close()
        publisher.close()
        assert client.closed == 1


class TestGeneratedFieldTable:
    def test_the_committed_table_matches_the_python_one(self) -> None:
        """The page types a tick from this file. A key renamed on one side
        only would reach production as an undefined price on a chart that
        still drew."""
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "scripts/dump_tick_fields.py", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_every_wire_key_is_unique(self) -> None:
        keys = [key for key, _, _, _ in TICK_FIELDS]
        assert len(keys) == len(set(keys))

    def test_every_column_is_on_live_ticks(self) -> None:
        """The table names columns, not concepts: a typo here would
        publish nulls for a field the database has."""
        from yfin.models.stream import LiveTick

        columns = set(LiveTick.__table__.c.keys())
        assert {column for _, column, _, _ in TICK_FIELDS} <= columns
