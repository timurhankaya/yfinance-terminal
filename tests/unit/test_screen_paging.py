"""`screener.fetch`'s call parameters and stop conditions, with a fake `yf.screen` that
records what was actually sent: given `offset`, `yf.screen` silently ignores `count` and
returns 25 rows, so checking only the result would miss it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from yfin.datasets.market import screener as mod
from yfin.datasets.market.base import MarketContext

FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)


class _Recorder:
    """Stands in for `yf.screen`; records the call parameters."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def __call__(self, query: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def _page(quotes: int, total: int, *, start: int = 0, meta: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "quotes": [{"symbol": f"S{start + i}"} for i in range(quotes)],
        "total": total,
        "count": quotes,
        "start": start,
    }
    if meta:
        body |= {"title": "Day Gainers", "id": "abc", "versionId": 12, "rawCriteria": "{}"}
    return body


def _run(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, key: str = "day_gainers") -> Any:
    monkeypatch.setattr(mod.yf, "screen", recorder)
    monkeypatch.setattr(mod, "call_yahoo", lambda fn, *, what: fn())
    ctx = MarketContext(
        fetched_at=FETCHED_AT,
        start=FETCHED_AT.date(),
        end=FETCHED_AT.date(),
    ).for_variant(key)
    return mod.ScreenerDataset().fetch(ctx)


class TestPagingParameters:
    def test_first_page_uses_count_not_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder([_page(5, 5, meta=True)])
        _run(monkeypatch, rec)
        assert "count" in rec.calls[0]
        assert "size" not in rec.calls[0]
        assert "offset" not in rec.calls[0]

    def test_later_pages_use_size_and_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An edit that sends `count` instead of `size` fetches 25 rows per page instead
        of 250, with no error; only the parameter actually sent shows it."""
        rec = _Recorder([_page(2, 6, meta=True), _page(2, 6, start=2), _page(2, 6, start=4)])
        _run(monkeypatch, rec)
        assert len(rec.calls) == 3
        for call in rec.calls[1:]:
            assert "size" in call, call
            assert "count" not in call, call
        assert [c["offset"] for c in rec.calls[1:]] == [2, 4]

    def test_sort_is_explicit_on_every_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`sortAsc`'s default is None -> descending. An unstable order across pages means
        overlap or a skipped symbol: a silently incomplete roster."""
        rec = _Recorder([_page(2, 6, meta=True), _page(2, 6, start=2), _page(2, 6, start=4)])
        _run(monkeypatch, rec, key="tr_equity")
        for call in rec.calls:
            assert call["sortField"] == "ticker"
            assert call["sortAsc"] is True

    def test_predefined_sends_key_custom_sends_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For predefined, the name is sent: it takes the GET path and
        fetches metadata. For custom, a query object is sent -- it has no name."""
        rec = _Recorder([_page(1, 1, meta=True)])
        _run(monkeypatch, rec, key="day_gainers")
        assert rec.calls[0]["query"] == "day_gainers"

        rec2 = _Recorder([_page(1, 1)])
        _run(monkeypatch, rec2, key="tr_equity")
        assert not isinstance(rec2.calls[0]["query"], str)


class TestMissingTotal:
    def test_a_page_without_total_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`screen_runs.total` is NOT NULL and a count Yahoo did not send is not 0."""
        page = _page(3, 3, meta=True)
        del page["total"]
        with pytest.raises(ValueError, match="no `total`"):
            _run(monkeypatch, _Recorder([page]))


class TestStopConditions:
    def test_stops_when_offset_reaches_total(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder([_page(3, 6, meta=True), _page(3, 6, start=3)])
        payload = _run(monkeypatch, rec)
        assert len(rec.calls) == 2
        assert payload.page_count == 2
        assert len(payload.quotes) == 6

    def test_stops_on_empty_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When `offset > total`, Yahoo returns 0 rows with no error; the empty-page branch
        catches this."""
        rec = _Recorder([_page(2, 99, meta=True), _page(0, 99, start=2)])
        payload = _run(monkeypatch, rec)
        assert len(rec.calls) == 2
        assert len(payload.quotes) == 2

    def test_stops_at_page_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`most_shorted_stocks` total=4,022 -> 17 pages. Without a limit, a
        single screen would eat half the request budget."""
        from yfin.core.config import get_settings

        limit = get_settings().yf_screen_max_pages
        rec = _Recorder([_page(2, 10_000, meta=True)] + [_page(2, 10_000)] * 20)
        payload = _run(monkeypatch, rec)
        assert len(rec.calls) == limit
        assert payload.page_count == limit

    def test_total_survives_pages_without_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`total` is taken from the first page and preserved: overwriting the first page's
        metadata would lose `screens.title`."""
        rec = _Recorder([_page(2, 4, meta=True), _page(2, 4, start=2)])
        payload = _run(monkeypatch, rec)
        assert payload.total == 4
        assert payload.metadata["title"] == "Day Gainers"


class TestVariantContract:
    def test_fetch_without_variant_raises(self) -> None:
        ctx = MarketContext(
            fetched_at=FETCHED_AT, start=FETCHED_AT.date(), end=FETCHED_AT.date()
        )
        with pytest.raises(ValueError, match="variant"):
            mod.ScreenerDataset().fetch(ctx)

    def test_unknown_screen_key_is_rejected(self) -> None:
        """No query body can be built for a name not in `screens.py`."""
        from yfin.core.config import Settings

        cfg = Settings(yf_screen_keys="nosuchscreen")
        with pytest.raises(ValueError, match="unknown screen"):
            mod.ScreenerDataset().variants(cfg, None)

    def test_variants_come_from_code_not_db(self) -> None:
        """The set comes from code; the DB only filters. Otherwise an empty `screens` table
        would mean no screen ever runs, and the bootstrap lock would never open."""
        from yfin.core.config import Settings
        from yfin.ingest.screens import ALL_SCREENS

        keys = mod.ScreenerDataset().variants(Settings(), None)
        assert keys == [s.key for s in ALL_SCREENS]
