"""sync_runs ve sync_run_items denetim tablolari (S5.2, S8.1)."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import BIGINT, SMALLINT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    Base,
    ProxyLabelType,
    RegionType,
    SymbolType,
    TsType,
)


class RunScope(enum.StrEnum):
    """Bir run'in kapsami: sembol dongusu mu, piyasa dongusu mu (S5.5)."""

    SYMBOLS = "symbols"
    MARKET = "market"
    # Sektor / endustri turu (SI S5.9). Ucuncu bir eksen: ne sembol ne
    # bolge dongusudur -- 156 anahtar, her biri kendi HTTP istegi.
    DOMAIN = "domain"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ItemStatus(enum.StrEnum):
    OK = "ok"
    EMPTY = "empty"  # kaynak veri yok - HATA DEGIL (S8.2)
    SKIPPED = "skipped"  # content_hash degismedi
    FAILED = "failed"
    UNKNOWN_SYMBOL = "unknown_symbol"
    # Kuyrukta islenmeden kaldi (shard cekildi/oldu). SKIPPED ile
    # KARISTIRILMAZ: onun anlami "content_hash degismedi"dir ve bu ayrim
    # olmasaydi cekilmemis sembol "veri guncel" sanilirdi.
    NOT_ATTEMPTED = "not_attempted"
    # Dataset o sembol icin BILEREK kosturulmadi (intraday_scope kapsami
    # disi, PB S6.5). NOT_ATTEMPTED ILE KARISTIRILMAZ: onun anlami "shard
    # cekildi, bu semboller ISLENMEDI" - yani gercek bir eksikliktir ve
    # RunTally.exit_code onu gorunce kosuyu PARTIAL yapar. Kapsam disilik
    # ise kasitli bir karardir; 4.500 sembole not_attempted yazmak
    # `yfin sync`i her gun exit 2 dondururdu.
    OUT_OF_SCOPE = "out_of_scope"


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # server_default mevcut satirlari geriye donuk etiketler; NOT NULL kisiti
    # ilk calistirmada patlamaz (ALGORITHM=INSTANT, 50k satirda 16 ms)
    scope: Mapped[RunScope] = mapped_column(
        Enum(RunScope, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=RunScope.SYMBOLS.value,
    )
    finished_at: Mapped[datetime | None] = mapped_column(TsType())
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dataset_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Proxy secimi bilindikten SONRA yazilir (P4.4)
    shard_count: Mapped[int] = mapped_column(SMALLINT, nullable=False, server_default="1")
    # Bu calistirmanin sembol evreni ve tarih araligi, insan-okunur biçimde
    # ("exchange=IST quote_type=EQUITY start=2020-01-01"). `scope` yalnizca
    # symbols/market ayrimini tasir; hangi run'in hangi evreni kapsadigi aksi
    # halde geriye donuk bilinemez ve eksiksizlik iddiasi denetlenemez
    # (AH S5.6). server_default YOKTUR: mevcut satirlar NULL kalir = filtresiz.
    selector: Mapped[str | None] = mapped_column(String(255, collation="C"))
    rows_fetched: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_verified: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_skipped: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class SyncRunItem(Base):
    """Cok tabloya yazan dataset'ler icin tablo basina bir satir yazilir."""

    __tablename__ = "sync_run_items"
    __table_args__ = (
        Index("ix_sync_run_items_run_status", "run_id", "status"),
        Index("ix_sync_run_items_symbol_dataset", "symbol", "dataset"),
        Index("ix_sync_run_items_proxy", "proxy_id", "status"),
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    # FK YOKTUR (S5.5): cozulemeyen sembol icin unknown_symbol kaydi
    # yazilamazdi (ERROR 1452). Denetim kaydi sembol silinse de kalmalidir.
    symbol: Mapped[str] = mapped_column(SymbolType(), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    table_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # Domain hucrelerinin bolge ekseni (SI S5.9). Sembol ve piyasa
    # tarafinda NULL kalir. `symbol` alanina domain SEMBOLU yazilir
    # (`^YH31130020`), anahtar DEGIL: kolon VARCHAR(32) ve bes endustri
    # anahtari bunu asiyor (en uzun 37).
    region: Mapped[str | None] = mapped_column(RegionType())

    shard_index: Mapped[int] = mapped_column(SMALLINT, nullable=False, server_default="0")
    # FK YOKTUR (P3.5) - `symbol` ile ayni gerekce ve bir tanesi daha:
    # InnoDB her INSERT icin ebeveyn proxies satirina shared lock alir;
    # shard binlerce item yazarken kendi proxy satirini S-kilitler ve
    # komsu shard'in saglik flush'i (X-lock) beklerdi. FK, tam da
    # engellemek istedigimiz deadlock'u uretirdi.
    proxy_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    # Anlik kopya: proxy silinse de denetim kaydi okunabilir kalir
    proxy_label: Mapped[str | None] = mapped_column(ProxyLabelType())
