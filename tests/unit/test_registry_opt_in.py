"""SQ K11: `opt_in` -- kayitli ama `all` genislemesine GIRMEYEN dataset.

Bu kavram, ilk tasarimin bir tuzagini kapatmak icin eklendi. Orada
`search`/`lookup` kaydi bir ayara baglanmisti (`sustainability` deseni) ve
iki kusuru vardi:

  1. Ayar kapaliyken `--datasets search` de calismiyordu; dataset registry'de
     HIC yoktu ve kullanici "bilinmeyen dataset" goruyordu.
  2. Ayar acildigi anda CIPLAK `yfin sync` de onlari cekmeye basliyordu --
     4.500 sembolde +9.000 istek/gun. Yani bayrak tuzagi COZMUYOR, yalnizca
     kullanici onu acana kadar erteliyordu.

`opt_in` ikisini birden cozer ve `bootstrap`in tam tersidir: `bootstrap` her
cozumlemeye EKLENIR, `opt_in` `all`dan CIKARILIR.
"""

from __future__ import annotations

from typing import Any

import pytest

from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import Registry


class _Ds(Dataset[None]):
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, ctx: SyncContext) -> None:  # pragma: no cover
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:  # pragma: no cover
        return NormalizedResult()


@pytest.fixture
def registry() -> Registry[Any]:
    reg: Registry[Any] = Registry(bootstrap="symbols")
    reg.register(_Ds("symbols"))
    reg.register(_Ds("info"))
    reg.register(_Ds("search"), opt_in=True)
    return reg


def _names(datasets: list[Any]) -> list[str]:
    return [d.name for d in datasets]


class TestAllExpansion:
    def test_opt_in_is_excluded_from_none(self, registry: Registry[Any]) -> None:
        assert _names(registry.resolve(None)) == ["symbols", "info"]

    def test_opt_in_is_excluded_from_literal_all(self, registry: Registry[Any]) -> None:
        assert "search" not in _names(registry.resolve(["all"]))

    def test_opt_in_is_excluded_from_empty_list(self, registry: Registry[Any]) -> None:
        assert "search" not in _names(registry.resolve([]))


class TestExplicitRequest:
    def test_named_opt_in_runs(self, registry: Registry[Any]) -> None:
        """Tuzagin ikinci yarisi: ADIYLA istendiginde CALISMALIDIR.

        Kayit bir ayara baglansaydi burada `UnknownDatasetError` alinirdi.
        """
        assert _names(registry.resolve(["search"])) == ["symbols", "search"]

    def test_opt_in_stays_user_visible(self, registry: Registry[Any]) -> None:
        """`yfin datasets` ciktisinda GORUNUR: kullanici adini bilmeden
        isteyemez."""
        assert "search" in registry.user_visible_names()

    def test_is_opt_in_reports_the_flag(self, registry: Registry[Any]) -> None:
        assert registry.is_opt_in("search") is True
        assert registry.is_opt_in("info") is False


class TestRegistrationHygiene:
    def test_reregistering_without_flag_clears_it(self, registry: Registry[Any]) -> None:
        """Ayni adi opt_in'siz yeniden kaydetmek bayragi TEMIZLER.

        Aksi halde bir kez opt_in yazilan ad, sonraki kayitta sessizce
        opt_in kalirdi ve `all` genislemesi beklenenden dar olurdu.
        """
        registry.register(_Ds("search"))
        assert registry.is_opt_in("search") is False
        assert "search" in _names(registry.resolve(None))

    def test_unregister_clears_the_flag(self, registry: Registry[Any]) -> None:
        registry.unregister("search")
        registry.register(_Ds("search"))
        assert registry.is_opt_in("search") is False

    def test_bootstrap_is_still_prepended(self, registry: Registry[Any]) -> None:
        """`opt_in` `bootstrap`i ETKILEMEZ: biri ekler, digeri cikarir."""
        assert _names(registry.resolve(["search"]))[0] == "symbols"


class TestRealRegistries:
    def test_bare_sync_excludes_discovery(self) -> None:
        """OLCULEN TUZAK: bu iki dataset `all`a girseydi 4.500 sembolde
        gunde +9.000 istek eklerdi."""
        from yfin.datasets.registry import SYMBOL_DATASETS

        names = _names(SYMBOL_DATASETS.resolve(None))
        assert "search" not in names
        assert "lookup" not in names

    def test_named_discovery_resolves(self) -> None:
        from yfin.datasets.registry import SYMBOL_DATASETS

        assert set(_names(SYMBOL_DATASETS.resolve(["search", "lookup"]))) == {
            "symbols",
            "search",
            "lookup",
        }

    def test_bare_market_sync_excludes_screener(self) -> None:
        """`yfin market sync`in maliyeti DEGISMEDI: screener'i cekseydi
        komut ~20 istekten ~50'ye cikardi."""
        from yfin.datasets.registry import MARKET_DATASETS

        assert "screener" not in _names(MARKET_DATASETS.resolve(None))
        assert _names(MARKET_DATASETS.resolve(["screener"])) == ["screener"]
