"""SyncContext.watermark'in `where` parametresi (PB S6.3).

price_bars'ta tek bir MAX(ts_utc) YANLIS CEVAP VERIR: 1m guncelken 60m
iki yil geride olabilir. Interval basina ayrilmazsa bars_60m "guncel"
sanilir ve ilk dolumu hic yapilmaz.

Mevcut cagrilar (history, shares_full) `where` VERMEZ ve davranislari
degismemelidir; bu dosya ikisini birden dogrular.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from yfin.datasets.base import SyncContext


class RecordingProvider:
    """Watermark cagrilarini kaydeden sahte saglayici."""

    def __init__(self, answers: dict[tuple[str, str, str, str], date | datetime] | None = None):
        self.calls: list[tuple[str, str, str, Mapping[str, Any] | None]] = []
        self.answers = answers or {}

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        self.calls.append((table, column, symbol, where))
        key = (table, column, symbol, str(where))
        return self.answers.get(key)


def _ctx(provider: RecordingProvider) -> SyncContext:
    return SyncContext(
        "AAPL",
        object(),
        datetime(2026, 9, 4, tzinfo=UTC),
        watermark_provider=provider,
    )


def test_where_reaches_the_provider() -> None:
    provider = RecordingProvider()
    ctx = _ctx(provider)

    ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "60m"})

    assert provider.calls == [("price_bars", "ts_utc", "AAPL", {"bar_interval": "60m"})]


def test_existing_calls_pass_no_where() -> None:
    """history/shares_full deseni: `where` verilmez, None olarak gecer."""
    provider = RecordingProvider()
    ctx = _ctx(provider)

    ctx.watermark("price_history", "session_date")

    assert provider.calls == [("price_history", "session_date", "AAPL", None)]


def test_intervals_are_isolated_from_each_other() -> None:
    """Bu testin varlik sebebi: interval boyutu olmadan 60m'nin
    watermark'i 1m'ninkiyle karisir ve ilk dolum hic yapilmaz."""
    provider = RecordingProvider(
        {
            ("price_bars", "ts_utc", "AAPL", "{'bar_interval': '1m'}"): datetime(2026, 9, 3),
            # 60m icin kayit YOK -> None -> ilk dolum
        }
    )
    ctx = _ctx(provider)

    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "1m"}) == datetime(
        2026, 9, 3
    )
    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "60m"}) is None


def test_full_refresh_still_short_circuits_with_where() -> None:
    """--full-refresh watermark'lari atlar; `where` bunu degistirmemeli."""
    provider = RecordingProvider()
    ctx = SyncContext(
        "AAPL",
        object(),
        datetime(2026, 9, 4, tzinfo=UTC),
        watermark_provider=provider,
        full_refresh=True,
    )

    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "1m"}) is None
    assert provider.calls == []
