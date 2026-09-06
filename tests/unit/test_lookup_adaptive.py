"""Proves the `lookup` call is adaptive.

This file is the regression lock on a design decision disproved during an
audit. The original version always made a single `all` call and had been
generalized from measuring one narrow term (`BTC`). For broad terms, `all`
truncates at ~1,000 documents: for `GOLD`, `lookupTotals.all` reports
7,273 while `documents` returns 995, and the typed union returns 3,313 --
the difference goes both ways.

Also tested: NOT falling back to the typed branch for a narrow term. If it
did, cost in the symbol loop would go from 1 to 8 requests per symbol,
adding +31,500 unnecessary requests per day across 4,500 symbols.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yfin.datasets.base import SyncContext
from yfin.datasets.discovery import lookup as mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "_discovery"
FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _block(name: str) -> dict[str, Any]:
    return _fixture(name)["finance"]["result"][0]


class _Recorder:
    """Stands in for `_fetch_type`; records the requested types."""

    def __init__(self, blocks: dict[str, dict[str, Any]]) -> None:
        self.blocks = blocks
        self.types: list[str] = []

    def __call__(self, term: str, lookup_type: str, count: int) -> dict[str, Any]:
        self.types.append(lookup_type)
        return self.blocks.get(lookup_type, {"documents": [], "lookupTotals": {}})


def _fetch(monkeypatch: pytest.MonkeyPatch, rec: _Recorder, term: str) -> Any:
    monkeypatch.setattr(mod, "_fetch_type", rec)
    ctx = SyncContext(symbol=term, ticker=None, fetched_at=FETCHED_AT)
    return mod.LookupDataset().fetch(ctx)


class TestAdaptiveBranch:
    def test_narrow_term_makes_exactly_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`BTC`: lookupTotals.all = 503, threshold 500... but that's borderline.

        A single call is expected for a genuinely narrow term below the
        threshold. Here a set similar to `AAPL` is simulated.
        """
        block = _block("lookup_BTC_all")
        block = {**block, "lookupTotals": {**block["lookupTotals"], "all": 57}}
        rec = _Recorder({"all": block})
        _fetch(monkeypatch, rec, "AAPL")
        # The claim is proved via call count, not a payload flag: a flag
        # is never written to the DB, so it would only be something the
        # test itself sees, giving a false sense of coverage.
        assert rec.types == ["all"]

    def test_broad_term_falls_back_to_typed_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: `GOLD` -> lookupTotals.all = 7,273, `all` returns 995 docs.

        Without falling back to the typed branch, over 70% of symbols
        would be lost with no visible error.
        """
        rec = _Recorder(
            {"all": _block("lookup_GOLD_all"), "equity": _block("lookup_GOLD_equity")}
        )
        _fetch(monkeypatch, rec, "GOLD")
        assert rec.types == ["all", *mod.TYPED_LOOKUPS]

    def test_typed_fallback_adds_to_all_it_does_not_replace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For `GOLD`, 354 symbols existed only in `all`.

        If the typed branch replaced `all` instead of adding to it, those
        symbols would be lost -- the difference goes both ways.
        """
        rec = _Recorder(
            {"all": _block("lookup_GOLD_all"), "equity": _block("lookup_GOLD_equity")}
        )
        payload = _fetch(monkeypatch, rec, "GOLD")
        sources = {t for t, _ in payload.documents}
        assert "all" in sources
        assert "equity" in sources

    def test_threshold_is_read_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The threshold is not hardcoded: adjust it via settings if the
        measurement changes."""
        from yfin.core.config import Settings

        monkeypatch.setenv("YF_LOOKUP_ALL_THRESHOLD", "10")
        assert Settings().yf_lookup_all_threshold == 10

    def test_totals_are_read_from_response_not_library_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The source reports nine types.

        `privateCompany` is not in the `LOOKUP_TYPES` constant; reading
        from the constant would never write that row.
        """
        rec = _Recorder({"all": _block("lookup_BTC_all")})
        payload = _fetch(monkeypatch, rec, "BTC")
        assert "privateCompany" in payload.totals

    def test_typed_lookups_excludes_all(self) -> None:
        assert mod.ALL_TYPE not in mod.TYPED_LOOKUPS
        assert len(mod.TYPED_LOOKUPS) == 7


class TestEnvelope:
    def test_non_dict_response_raises(self) -> None:
        """If the shape changes, `failed`; returning empty silently would
        conflate "no data" with "the response shape changed"."""
        with pytest.raises(TypeError):
            mod._result_block([1, 2, 3])

    def test_empty_result_list_is_empty_block(self) -> None:
        assert mod._result_block({"finance": {"result": []}}) == {}
