"""Dataset registry ve cozumleyici (S6.1).

Alias genisletme, sira koruyan tekillestirme, topolojik siralama, dongu ve
bilinmeyen-ad kontrolu tek bir `Registry` sinifindadir. UC ornek uretilir:
sembol-kapsamli (`SYMBOL_DATASETS`), piyasa-kapsamli (`MARKET_DATASETS`) ve
sektor/endustri kapsamli (`DOMAIN_DATASETS`) dataset'ler. Ucu ayni
sozlesmeyi paylasir; tek fark bootstrap dataset'inin varligidir.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from yfin.datasets.base import Dataset
    from yfin.datasets.domain.base import DomainDataset
    from yfin.datasets.market.base import GlobalDataset


class Registrable(Protocol):
    """Registry'nin gordugu tek arayuz."""

    name: str
    depends_on: tuple[str, ...]


class UnknownDatasetError(ValueError):
    pass


class DependencyCycleError(ValueError):
    pass


class Registry[D: Registrable]:
    """Ad -> dataset sozlugu ve cozumleyicisi.

    `bootstrap` verilirse o dataset kullanici secmese de her cozumlemede
    basa eklenir (sembol tarafinda `symbols`). `bootstrap=None` piyasa
    tarafi icindir: zorunlu on dataset yoktur.
    """

    def __init__(
        self,
        *,
        bootstrap: str | None = None,
        aliases: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.bootstrap = bootstrap
        self.aliases: dict[str, tuple[str, ...]] = dict(aliases or {})
        self._items: dict[str, D] = {}
        # ADIYLA ISTENMEDIKCE kosmayan dataset'ler (SQ K11). `bootstrap`in
        # tersi: o her cozumlemeye EKLENIR, bunlar `all`dan CIKARILIR.
        self._opt_in: set[str] = set()

    # --- kayit ------------------------------------------------------------

    def register(self, ds: D, *, opt_in: bool = False) -> D:
        """`opt_in=True`: kayitli ama `all` genislemesine GIRMEZ.

        Gerekce olculmus bir tuzaktir (SQ K11). `search` ve `lookup` sembol
        basina birer istek ekler; 4.500 sembolde +9.000 istek/gun. Bunlar
        kosulsuz kayitli olsaydi ciplak `yfin sync` onlari da cekerdi.

        ILK COZUM YANLISTI: kaydi bir ayara baglamak (`sustainability`
        deseni). O, dataset'i `--datasets search` ile de erisilemez yapiyor
        ve ayar acildigi anda ciplak kosu YINE agirlasiyordu -- yani tuzagi
        cozmuyor, yalnizca erteliyordu.

        `opt_in` ikisini birden cozer: ad verildiginde calisir, `all`
        genislemesinde HIC gorunmez. Bildirim dataset'in KAYIT YERINDE
        durur, registry'de gomulu bir ad listesinde degil (OCP).
        """
        self._items[ds.name] = ds
        if opt_in:
            self._opt_in.add(ds.name)
        else:
            self._opt_in.discard(ds.name)
        return ds

    def is_opt_in(self, name: str) -> bool:
        return name in self._opt_in

    def unregister(self, name: str) -> None:
        """Yalnizca test icin; kayitli olmayan ad sessizce yok sayilir."""
        self._items.pop(name, None)
        self._opt_in.discard(name)

    # Koleksiyon protokolu: `name in registry`, `registry[name]`,
    # `len(registry)`, `for name in registry`. Ayrica names()/values()
    # tutmak ayni bilgiye ikinci bir yuz acardi.
    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __getitem__(self, name: str) -> D:
        return self._items[name]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def get(self, name: str) -> D | None:
        return self._items.get(name)

    def user_visible_names(self) -> list[str]:
        """Kullanicinin --datasets ile verebilecegi adlar: kayitlar (bootstrap
        haric) + alias'lar."""
        visible = set(self._items) | set(self.aliases)
        if self.bootstrap is not None:
            visible -= {self.bootstrap}
        return sorted(visible)

    # --- cozumleme --------------------------------------------------------

    def _expand(self, names: Sequence[str]) -> list[str]:
        """Alias'lari genisletir, SIRA KORUNARAK tekillestirir."""
        out: dict[str, None] = {}
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            if name in self.aliases:
                for target in self.aliases[name]:
                    out[target] = None
                continue
            out[name] = None
        return list(out)

    def resolve(self, names: Sequence[str] | None) -> list[D]:
        """None, bos liste veya 'all' -> OPT-IN OLMAYANLARIN hepsi.

        Alias'lari genisletir, sira koruyarak tekillestirir, depends_on'a
        gore topolojik siralar, bilinmeyen adi reddeder, donguyu yakalar.
        Bootstrap tanimliysa her zaman basa eklenir.
        """
        if names is None or not names or (len(names) == 1 and names[0].strip() == "all"):
            selected = [
                n for n in self._items if n != self.bootstrap and n not in self._opt_in
            ]
        else:
            selected = self._expand(names)

        unknown = [n for n in selected if n not in self._items]
        if unknown:
            raise UnknownDatasetError(
                f"bilinmeyen dataset: {', '.join(unknown)}. "
                f"gecerli adlar: {', '.join(self.user_visible_names())}"
            )

        wanted: dict[str, None] = {}
        if self.bootstrap is not None:
            wanted[self.bootstrap] = None
        for name in selected:
            wanted[name] = None

        ordered: list[str] = []
        state: dict[str, int] = {}  # 0=ziyaret ediliyor, 1=bitti

        def visit(name: str, path: tuple[str, ...]) -> None:
            if state.get(name) == 1:
                return
            if state.get(name) == 0:
                raise DependencyCycleError(f"dongu: {' -> '.join((*path, name))}")
            if name not in self._items:
                raise UnknownDatasetError(f"bilinmeyen bagimlilik: {name}")
            state[name] = 0
            for dep in self._items[name].depends_on:
                visit(dep, (*path, name))
            state[name] = 1
            ordered.append(name)

        for name in wanted:
            visit(name, ())

        return [self._items[n] for n in ordered]


# --- ornekler --------------------------------------------------------------

# 'actions' ve 'financials' registry'de kayit degil, ALIAS'tir (S6.1).
SYMBOL_DATASETS: Registry[Dataset[Any]] = Registry(
    bootstrap="symbols",
    aliases={
        "actions": ("dividends", "splits", "capital_gains"),
        # price_bars dataset ailesi (PB S6.1). `bars`in genisledigi
        # interval kumesi YF_BAR_INTERVALS ile daraltilabilir; kayitlarin
        # kendisi her zaman altisi birdendir.
        "bars": (
            "bars_1m",
            "bars_5m",
            "bars_15m",
            "bars_60m",
            "bars_1wk",
            "bars_1mo",
        ),
        "intraday": ("bars_1m", "bars_5m", "bars_15m", "bars_60m"),
        # `recommendations_summary` KAYIT DEGIL ALIAS'tir: kaynakta govdesi
        # `return self.get_recommendations(as_dict=as_dict)` (base.py:220).
        "recommendations_summary": ("recommendations",),
        # `sustainability` DAHIL DEGILDIR: izleme dataset'idir, tablosu yok
        # ve varsayilan olarak hic kayitli olmaz (AH S6.3).
        "analysis": (
            "recommendations",
            "upgrades_downgrades",
            "analyst_price_targets",
            "earnings_estimate",
            "revenue_estimate",
            "eps_trend",
            "eps_revisions",
            "earnings_history",
            "growth_estimates",
        ),
        "holders": (
            "major_holders",
            "institutional_holders",
            "mutualfund_holders",
            "insider_purchases",
            "insider_transactions",
            "insider_roster_holders",
        ),
        "funds": ("funds_data",),
        # `valuation` AYRI bir alias'tir, `financials`in parcasi DEGIL:
        # kaynak dokumantasyonun Financials bolumunde yer almaz ve ayri bir
        # HTTP istegidir; `financials`e katmak o adin mevcut maliyetini
        # sessizce iki istek buyuturdu.
        "valuation": ("valuation_measures", "quarterly_valuation_measures"),
        "financials": (
            "income_stmt",
            "quarterly_income_stmt",
            "ttm_income_stmt",
            "balance_sheet",
            "quarterly_balance_sheet",
            "cashflow",
            "quarterly_cashflow",
            "ttm_cashflow",
        ),
    },
)

MARKET_DATASETS: Registry[GlobalDataset[Any]] = Registry(
    bootstrap=None,
    aliases={
        "market": ("market_status", "market_summary"),
        "calendars": (
            "earnings_calendar",
            "economic_calendar",
            "ipo_calendar",
            "splits_calendar",
        ),
    },
)


# Ucuncu registry (SI S6.3). `Registry` sinifi DEGISMEZ: `Registrable`
# protokolu `name` + `depends_on` istiyor, `DomainDataset` ikisini de
# tasiyor.
DOMAIN_DATASETS: Registry[DomainDataset[Any]] = Registry(
    bootstrap="domain_taxonomy",
    aliases={
        "sector": ("sector_profile", "sector_rankings"),
        "industry": ("industry_profile", "industry_rankings"),
    },
)


def register(ds: Dataset[Any], *, opt_in: bool = False) -> Dataset[Any]:
    """Sembol-kapsamli dataset kaydi (dataset modullerinin dekoratoru).

    `opt_in=True` -> `all` genislemesine girmez; bkz. `Registry.register`.
    """
    return SYMBOL_DATASETS.register(ds, opt_in=opt_in)


def register_market(ds: GlobalDataset[Any], *, opt_in: bool = False) -> GlobalDataset[Any]:
    """Piyasa-kapsamli dataset kaydi."""
    return MARKET_DATASETS.register(ds, opt_in=opt_in)


def register_domain(ds: DomainDataset[Any]) -> DomainDataset[Any]:
    """Sektor / endustri kapsamli dataset kaydi."""
    return DOMAIN_DATASETS.register(ds)
