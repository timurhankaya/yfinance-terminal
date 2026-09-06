"""SQ K6: `lookup` cagrisinin ADAPTIF oldugunu surer.

Bu dosya, tasarimin denetimde CURUTULEN kararinin regresyon kilidi. Ilk
hali "her zaman tek `all` cagrisi" idi ve tek bir DAR terimle (`BTC`)
olculup genellenmisti. Genis terimlerde `all` ~1.000 belgede kirpiliyor:
`GOLD` icin `lookupTotals.all` 7.273 bildirirken `documents` 995 donuyor,
tipli birlesim ise 3.313 -- ve fark IKI YONLU.

Dar terimde tipli dala GECMEMEK de test edilir: gecilseydi sembol
dongusunde maliyet 1 yerine 8 istek/sembol olur, 4.500 sembolde gunde
+31.500 gereksiz istek eklenirdi.
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
    """`_fetch_type` yerine gecer; istenen tipleri kaydeder."""

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
        """`BTC`: lookupTotals.all = 503, esik 500... ama bu SINIRDA.

        Esigin altindaki gercek bir dar terim icin tek cagri beklenir.
        Burada `AAPL` benzeri bir kume taklit edilir.
        """
        block = _block("lookup_BTC_all")
        block = {**block, "lookupTotals": {**block["lookupTotals"], "all": 57}}
        rec = _Recorder({"all": block})
        _fetch(monkeypatch, rec, "AAPL")
        # Iddia CAGRI SAYISI uzerinden surulur, bir payload bayragi
        # uzerinden degil: bayrak DB'ye yazilmadigi icin yalnizca testin
        # gordugu bir sey olurdu ve "denetim alani" izlenimi yaratirdi.
        assert rec.types == ["all"]

    def test_broad_term_falls_back_to_typed_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESYON: `GOLD` -> lookupTotals.all = 7.273, `all` 995 belge.

        Tipli dala gecilmezse sembollerin %70'inden fazlasi KAYBEDILIR ve
        hicbir hata gorulmez.
        """
        rec = _Recorder(
            {"all": _block("lookup_GOLD_all"), "equity": _block("lookup_GOLD_equity")}
        )
        _fetch(monkeypatch, rec, "GOLD")
        assert rec.types == ["all", *mod.TYPED_LOOKUPS]

    def test_typed_fallback_adds_to_all_it_does_not_replace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`GOLD`da 354 sembol YALNIZ `all`da vardi.

        Tipli dal `all`i birakip yerine gecseydi o semboller kaybolurdu --
        fark IKI YONLU.
        """
        rec = _Recorder(
            {"all": _block("lookup_GOLD_all"), "equity": _block("lookup_GOLD_equity")}
        )
        payload = _fetch(monkeypatch, rec, "GOLD")
        sources = {t for t, _ in payload.documents}
        assert "all" in sources
        assert "equity" in sources

    def test_threshold_is_read_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Esik kodda gomulu DEGIL: olcum degistiginde ayardan duzeltilir."""
        from yfin.config import Settings

        monkeypatch.setenv("YF_LOOKUP_ALL_THRESHOLD", "10")
        assert Settings().yf_lookup_all_threshold == 10

    def test_totals_are_read_from_response_not_library_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQ S4.1/10: kaynak DOKUZ tip bildiriyor.

        `privateCompany` `LOOKUP_TYPES` sabitinde YOK; sabitten okunsaydi
        o satir hic yazilmazdi.
        """
        rec = _Recorder({"all": _block("lookup_BTC_all")})
        payload = _fetch(monkeypatch, rec, "BTC")
        assert "privateCompany" in payload.totals

    def test_typed_lookups_excludes_all(self) -> None:
        assert mod.ALL_TYPE not in mod.TYPED_LOOKUPS
        assert len(mod.TYPED_LOOKUPS) == 7


class TestEnvelope:
    def test_non_dict_response_raises(self) -> None:
        """Sekil degisirse `failed`; sessizce bos donmek "veri yok" ile
        "yanit sekli degisti"yi karistirirdi."""
        with pytest.raises(TypeError):
            mod._result_block([1, 2, 3])

    def test_empty_result_list_is_empty_block(self) -> None:
        assert mod._result_block({"finance": {"result": []}}) == {}
