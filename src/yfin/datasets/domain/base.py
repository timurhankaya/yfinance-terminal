"""Domain (sektor / endustri) kapsamli dataset sozlesmesi (SI S6.1).

`GlobalDataset`'in kardesi. `SyncContext`'ten TUREMEZ: `symbol` alani domain
tarafinda yanlis anlam tasirdi -- ve burada bu, `market/base.py`'deki
gerekceden DAHA KESKINDIR, cunku domain tablolarinda `symbol` GERCEKTEN
VARDIR ama SIRKETIN sembolüdür.

Ucuncu eksen: ne sembol ne bolge dongusu. 156 anahtar, her biri kendi HTTP
istegi. Bolge dongusu dataset'in DISINDADIR (domain_runner icinde), boylece
`sync_run_items` granulerligi dogal olarak (dataset x anahtar x bolge x
tablo) olur.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from yfin.datasets.asof_base import (
    DOMAIN_GATE_KEY_COLUMNS,
    DOMAIN_GATE_TABLE,
    GLOBAL_REGION_MARKER,
    AsOfGate,
    _first_row,
)
from yfin.datasets.base import NormalizedResult, WriteStats
from yfin.persistence import RowWriter, apply_write

DomainType = Literal["sector", "industry"]


@dataclass
class DomainContext:
    """Run'a ait, SEMBOLSUZ ve anahtar-hedefli baglam.

    `for_target` ve `for_region` AYNI `_cache`'i paylasir
    (`MarketContext.for_region` deseni): bir (anahtar, bolge) ciftinin ham
    JSON'u BIR KEZ cekilir ve o ciftin TUM dataset'lerini besler. Bolgesiz
    dataset'ler BIRINCIL bolgenin yanitini kullanir ve ek istek uretmezler
    (veri bolgeden bagimsiz olculdu, SI S4.5).
    """

    fetched_at: datetime
    as_of_date: date
    primary_region: str
    region: str = GLOBAL_REGION_MARKER
    key: str | None = None
    domain_type: DomainType | None = None
    # Endustri anahtari -> DB'deki ebeveyn sektor anahtari. Runner
    # `domain_taxonomy` turundan SONRA DB'den doldurur; `industry_profile`
    # yanittaki `sectorKey` ile karsilastirip uyusmazlikta WARNING uretir
    # (taksonomi kaymasi sinyali, SI S7.3).
    parents: dict[str, str] = field(default_factory=dict, repr=False)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _clone(self, **changes: Any) -> DomainContext:
        clone = DomainContext(
            fetched_at=self.fetched_at,
            as_of_date=self.as_of_date,
            primary_region=self.primary_region,
            region=changes.get("region", self.region),
            key=changes.get("key", self.key),
            domain_type=changes.get("domain_type", self.domain_type),
            parents=self.parents,
        )
        clone._cache = self._cache
        return clone

    def for_target(self, key: str, domain_type: DomainType) -> DomainContext:
        return self._clone(key=key, domain_type=domain_type)

    def for_region(self, region: str) -> DomainContext:
        return self._clone(region=region)

    # --- cekim icin turetilmis degerler -----------------------------------

    @property
    def fetch_region(self) -> str:
        """Fiilen istenecek bolge.

        Bolgesiz turda `region` `'*'`tir; o istek BIRINCIL bolgeye gider ve
        onbellegi bolgeli turlarla PAYLASIR -- birincil bolge zaten
        cekiliyorsa ikinci bir HTTP istegi yapilmaz.
        """
        return self.primary_region if self.region == GLOBAL_REGION_MARKER else self.region

    @property
    def target_key(self) -> str:
        if self.key is None:  # pragma: no cover - savunma
            raise ValueError("DomainContext.key ayarlanmadan fetch cagrildi")
        return self.key

    @property
    def target_type(self) -> DomainType:
        if self.domain_type is None:  # pragma: no cover - savunma
            raise ValueError("DomainContext.domain_type ayarlanmadan fetch cagrildi")
        return self.domain_type


class DomainDataset[RawT](ABC):
    """Tek bir (anahtar, bolge) ciftinin cekilmesi, normalize edilmesi, yazilmasi."""

    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()  # yazdigi TABLO adlari
    # Hangi anahtar kumesi uzerinde doner
    scope: DomainType = "sector"
    # True ise BOLGE dongusune girer; False ise tek tur (`region='*'`)
    regional: bool = False
    # False ise dataset ANAHTAR DONGUSUNE GIRMEZ ve tek turda kosar.
    # Yalnizca bootstrap (`domain_taxonomy`) boyledir: 156 `symbols` +
    # 156 `domains` satiri TEK TRANSACTION'da yazilir -- taksonomi ya
    # butun olarak tutarlidir ya hic (SI S6.7).
    per_key: bool = True

    @abstractmethod
    def fetch(self, ctx: DomainContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT, key: str) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats


class DomainAsOfDataset[RawT](AsOfGate, DomainDataset[RawT]):
    """as-of kapili domain dataset'i.

    `AsOfDataset`'ten TUREYEMEZ (SI S6.2): o `Dataset[RawT]`'in altindadir ve
    `fetch(SyncContext)` / `normalize(raw, symbol)` imzasini tasir. Ortak
    olan sey kapi mantigidir, hiyerarsi degil -- bu yuzden `AsOfGate` bir
    mixin'dir.
    """

    asof_gate_table = DOMAIN_GATE_TABLE
    asof_gate_key_columns = DOMAIN_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Kapi anahtari: (domain_key, dataset, region).

        `region` dataset ORNEGINDE DURUM OLARAK TUTULMAZ (registry'deki
        dataset'ler tekildir ve bolge dongusu onlari yeniden kullanir); tur
        basina `normalize()`'a `DomainContext`'ten gecen deger SATIRLARA
        yazilir ve buradan geri okunur. Bolgesiz dataset'lerin satirlarinda
        `region` kolonu yoktur -> `'*'`.
        """
        first = _first_row(result)
        return {
            "domain_key": first["domain_key"],
            "dataset": self.name,
            "region": first.get("region", GLOBAL_REGION_MARKER),
        }
