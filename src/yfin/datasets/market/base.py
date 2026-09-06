"""Piyasa-kapsamli dataset sozlesmesi (S6.4).

`MarketContext` `SyncContext`'ten TUREMEZ: ortak bir taban sinif, `symbol`
alaninin piyasa tarafinda var sanilmasina yol acardi. Tek fark
`normalize`'in `symbol` almamasidir; TableWrite, NormalizedResult,
WriteStats, RowWriter ve snapshot mantigi paylasilir.

Bolge dongusu dataset'in DISINDADIR (market_runner icinde): boylece
sync_run_items granulerligi dogal olarak (dataset x tablo x bolge) olur ve
WriteStats'in bolge kirilimi tasimasi gerekmez.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from yfin.datasets.base import NormalizedResult, WriteStats
from yfin.datasets.snapshot_base import snapshot_upsert
from yfin.persistence import RowWriter, apply_write

# SQ K2: ucuncu deger. Ekran dongusu de dataset'in DISINDA doner -- bolge
# dongusunun bu modulun basindaki gerekcesi kelimesi kelimesine gecerlidir:
# `sync_run_items` granulerligi dogal olarak (dataset x ekran x tablo) olur
# ve bir ekranin patlamasi komsu ekrani `failed` gostermez.
MarketScope = Literal["global", "region", "variant"]


@dataclass
class MarketContext:
    """Run'a ait, sembolsuz baglam."""

    fetched_at: datetime
    start: date
    end: date
    region: str | None = None
    # SQ S6.1: `scope="variant"` dis dongusunun o turdeki anahtari (ekran
    # adi). `region`dan AYRI bir alandir cunku ekran bir bolge degildir ve
    # `sync_run_items.region` bolge semantigi tasir.
    variant: str | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """Calendars ornegi dort takvim dataset'i arasinda, Market ornegi
        ise bolge basina bir kez paylasilir."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _clone(self, **changes: Any) -> MarketContext:
        """Onbellegi PAYLASAN kopya -- tek klonlama noktasi.

        `for_region` eskiden alanlari ELLE sayiyordu. `variant` eklenip orada
        da sayilmasaydi bolge dalinda SESSIZCE duserdi; yeni her alan ayni
        tuzagi kurardi. `DomainContext._clone` (domain/base.py) ayni sorunu
        ayni bicimde cozuyor.
        """
        clone = MarketContext(
            fetched_at=changes.get("fetched_at", self.fetched_at),
            start=changes.get("start", self.start),
            end=changes.get("end", self.end),
            region=changes.get("region", self.region),
            variant=changes.get("variant", self.variant),
        )
        clone._cache = self._cache
        return clone

    def for_region(self, region: str) -> MarketContext:
        """Ayni onbellegi paylasan, bolgesi ayarlanmis baglam."""
        return self._clone(region=region)

    def for_variant(self, variant: str) -> MarketContext:
        """Ayni onbellegi paylasan, varyanti ayarlanmis baglam (SQ S6.1).

        `region` alanina YAZILMAZ: ekran bir bolge degildir.
        """
        return self._clone(variant=variant)


class GlobalDataset[RawT](ABC):
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    scope: MarketScope = "global"

    def variants(self, settings: Any, session: Any) -> Sequence[str]:
        """`scope == "variant"` ise dis dongunun anahtarlari (SQ S6.1).

        `session` ZORUNLUDUR ve imzanin en tartismali parcasidir: varyant
        kumesi `screens` tablosundaki `is_enabled` kolonuna bakar. Yalniz
        `settings` alan bir imza DB'yi okuyamazdi -- `Settings` bir Pydantic
        ayar nesnesidir.

        `market_regions()` gibi runner'da DEGIL dataset'te durur: ekran
        kumesini bilen taraf dataset'tir, runner'in `screens` tablosundan
        haberi olmasi gerekmez.
        """
        return ()

    @abstractmethod
    def fetch(self, mctx: MarketContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats


class SnapshotGlobalDataset[RawT](GlobalDataset[RawT]):
    """Snapshot + gecmis yazan piyasa dataset'i.

    Sembol tarafindaki `SnapshotDataset` ile AYNI politikayi paylasir
    (S6.3/a): karsilastirma snapshot tablosunda, yazma _history'ye; anahtar
    kolonlari bildirimseldir (market_status icin ("region",),
    market_summary icin ("region", "board_code")).
    """

    snapshot_table: str
    history_table: str
    key_columns: tuple[str, ...]

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        return snapshot_upsert(
            writer,
            result,
            snapshot_table=self.snapshot_table,
            history_table=self.history_table,
            key_columns=self.key_columns,
        )
