"""Sahiplik ve insider tablolari (AH S5.2).

Bes tablonun ucu AS-OF (kaynak "su anki ilk 10 / mevcut kadro" donduruyor),
biri (insider_transactions) kaynagin kendi tarihini tasidigi icin as-of
degildir.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    KeyTextType,
    PersonNameType,
    PriceType,
    ShortHashType,
    TsType,
    symbol_fk_column,
)


class HolderType(enum.StrEnum):
    INSTITUTION = "institution"
    MUTUALFUND = "mutualfund"


HOLDER_TYPE_ENUM = Enum(
    HolderType,
    values_callable=lambda e: [m.value for m in e],
    name="holder_type",
    native_enum=True,
)


class HolderBreakdown(Base):
    """majorHoldersBreakdown: 19/19 sembolde ayni 4 anahtar."""

    __tablename__ = "holder_breakdown"
    __table_args__ = (
        Index("ix_holder_breakdown_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    insiders_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    institutions_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    institutions_float_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    # Kaynakta float (7750.0) geliyor
    institutions_count: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InstitutionalHolder(Base):
    """institutional_holders + mutualfund_holders TEK tabloda.

    14 sembolde kolon setleri BIREBIR ayni olculdu. Iki dataset ayni tabloya
    yazar; kapsamlari `scope_columns=(symbol, as_of_date, holder_type)` ile
    ayrisir -- scope_columns'in var olma nedeni tam olarak budur.
    """

    __tablename__ = "institutional_holders"
    __table_args__ = (
        Index("ix_institutional_holders_holder", "holder"),
        Index("ix_institutional_holders_reported", "date_reported"),
        Index("ix_institutional_holders_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    holder_type: Mapped[HolderType] = mapped_column(HOLDER_TYPE_ENUM, primary_key=True)
    # Olculen max 70 (JPM mutualfund). PK toplami 548 byte (MySQL'de olculdu).
    holder: Mapped[str] = mapped_column(KeyTextType(128), primary_key=True)
    # SATIR BAZINDA degisir: AAPL mutualfund'da tek listede 4 farkli tarih.
    # NULL kabul eder: hic bos gelmedigi OLCULMEDI ve sembol basina tek
    # transaction geregi tek bir NaT tum sembolu rollback ederdi.
    date_reported: Mapped[date | None] = mapped_column(Date)
    pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    pct_change: Mapped[Decimal | None] = mapped_column(PriceType())
    # Olculen max: shares 1.94e9, value 1.76e13
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderActivity(Base):
    """netSharePurchaseActivity'nin 7 satirlik sunumu TEK satira pivotlanir.

    Kaynak zaten tek bir kaydin 7 satirlik gosterimidir; 0. kolonun ADI
    dinamiktir ('Insider Purchases Last 6m'), bu yuzden satir etiketi ADDAN
    degil KONUMDAN okunur ve donem eki period_label'a ayristirilir.
    """

    __tablename__ = "insider_activity"
    __table_args__ = (
        Index("ix_insider_activity_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period_label: Mapped[str] = mapped_column(AsciiKeyType(8), nullable=False)
    # NEGATIF olabilir (KO net -547_806) -> isaretli DECIMAL(38,0)
    purchases_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    sales_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    net_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    total_insider_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    # SIGNED Integer: net islem sayisi mantiken negatif olabilir ve bu
    # olculmedi; UNSIGNED olsaydi ERROR 1264 tum sembolu dusururdu.
    purchases_trans: Mapped[int | None] = mapped_column(Integer)
    sales_trans: Mapped[int | None] = mapped_column(Integer)
    net_trans: Mapped[int | None] = mapped_column(Integer)
    net_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    buy_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    sell_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderTransaction(Base):
    """Insider islemleri. AS-OF DEGIL: kaynak islem tarihini veriyor.

    fact_hash PK'ya girer AMA TEK BASINA YETMEZ: PFE'de dokuz kolonun
    TAMAMINDA ozdes iki satir olculdu (BOSHOFF CHRISTOFFEL, 8741 hisse,
    263716 deger, 2025-02-21) ve hash'leri de ozdes. Bu yuzden normalize
    ONCE birebir tekillestirme yapar (AH S8.3); aksi halde 34 satir okunup
    33 yazilir ve S8.5 dogrulamasi her calistirmada kirilirdi.
    """

    __tablename__ = "insider_transactions"
    __table_args__ = (
        Index("ix_insider_transactions_start", "start_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fact_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    # Her zaman kisi adi DEGIL: 'Elliott Investment Management L.P' (BP.L).
    # Olculen max 33.
    insider: Mapped[str | None] = mapped_column(PersonNameType())
    # Olculen max 56 (WMT); '' -> NULL (BP.L'de bos olculdu)
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    text: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # 16 sembol / 1464 satirin HEPSINDE '' -> NULL. Kolon yine de acilir ki
    # uc dolmaya basladiginda migration gerekmesin.
    transaction_label: Mapped[str | None] = mapped_column(String(64, collation="C"))
    url: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    # DIS ve BP.L'de TUM satirlarda NaN
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    # 'D', 'I' ve 'D/I' (XOM) -> VARCHAR(2) yetersizdi
    ownership: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderRosterHolder(Base):
    """Mevcut insider kadrosu (9-10 kisi).

    Kaynak kolon seti sembole gore 7 / 9 / 11'dir ve SIRASI da sabit degildir
    -> normalize row.get(...) kullanir. positionSummary/positionSummaryDate
    yalnizca NVDA'da goruldu ama orada bir kisinin TEK hisse bilgisiydi;
    kolona alinmasaydi o satirin tum hisse alanlari NULL kalirdi.
    """

    __tablename__ = "insider_roster"
    __table_args__ = (
        Index("ix_insider_roster_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # PK toplami 1055 byte (MySQL'de olculdu, sinir 3072)
    name: Mapped[str] = mapped_column(PersonNameType(), primary_key=True)
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    url: Mapped[str | None] = mapped_column(Text)
    most_recent_transaction: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # datetime64 VEYA ham epoch float64 gelebilir (6 sembolde dolu float
    # olculdu); kinds.py::_to_datetime iki bicimi de kabul eder.
    latest_transaction_date: Mapped[datetime | None] = mapped_column(TsType())
    position_direct_date: Mapped[datetime | None] = mapped_column(TsType())
    position_indirect_date: Mapped[datetime | None] = mapped_column(TsType())
    shares_owned_directly: Mapped[Decimal | None] = mapped_column(BigNumType())
    shares_owned_indirectly: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary_date: Mapped[datetime | None] = mapped_column(TsType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
