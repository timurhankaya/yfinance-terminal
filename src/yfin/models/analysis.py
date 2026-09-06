"""Analist tablolari (AH S5.1).

Sekiz tablonun altisi AS-OF'tur: kaynak yalnizca "su an"i donduruyor ve donem
etiketi GORELI (0q, +1y, 0m, -1m) -- yani `as_of_date` olmadan veri
anlamsizdir. Ikisi (analyst_grade_changes, earnings_history) kaynagin kendi
tarihini tasidigi icin as-of DEGILDIR.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    Enum,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    FactValueType,
    KeyTextType,
    PriceType,
    TsType,
    symbol_fk_column,
)

# Goreli donem etiketi: '0q', '+1q', '0y', '+1y', 'LTG' / '0m'..'-3m'
PERIOD_LENGTH = 8


class EstimateMetric(enum.StrEnum):
    EPS = "eps"
    REVENUE = "revenue"


# ENUM tanimi tek kaynaktan. PostgreSQL'de deger sirasi ordinal DEGILDIR
# (pg_enum OID'i saklanir), yani MySQL'deki sessiz-yanlis-deger tuzagi
# yoktur; tek kaynak yine de BAKIM icin korunur.
METRIC_ENUM = Enum(
    EstimateMetric,
    values_callable=lambda e: [m.value for m in e],
    name="estimate_metric",
    native_enum=True,
)


class AnalystRecommendation(Base):
    """strongBuy..strongSell sayaclari; satir sayisi 3 VEYA 4 (olculdu)."""

    __tablename__ = "analyst_recommendations"
    __table_args__ = (
        Index("ix_analyst_recommendations_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    # 19 sembolde de int64, NaN yok -> NOT NULL savunulabilir
    strong_buy: Mapped[int] = mapped_column(Integer, nullable=False)
    buy: Mapped[int] = mapped_column(Integer, nullable=False)
    hold: Mapped[int] = mapped_column(Integer, nullable=False)
    sell: Mapped[int] = mapped_column(Integer, nullable=False)
    strong_sell: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystGradeChange(Base):
    """Analist not degisiklikleri. AS-OF DEGIL: kaynak kendi tarihini tasiyor.

    Saf upsert'tir. `replace_scope` olsaydi ~1000 satirlik tavan disinda kalan
    eski kayitlar her calistirmada silinirdi (AH S4.5).
    """

    __tablename__ = "analyst_grade_changes"
    __table_args__ = (
        Index("ix_analyst_grade_changes_ts", "grade_ts_utc"),
        Index("ix_analyst_grade_changes_firm", "firm"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    # Kaynak tz-naive ama epochGradeDate SANIYESINDEN uretilmis -> UTC'dir;
    # ikinci bir tz donusumu yapilmaz.
    grade_ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    # 15 sembol / 8852 satirda (GradeDate, Firm) dup=0; olculen max 26.
    firm: Mapped[str] = mapped_column(KeyTextType(64), primary_key=True)
    to_grade: Mapped[str | None] = mapped_column(String(32, collation="C"))
    from_grade: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # ENUM DEGIL: 5 deger olculdu, bu Yahoo'nun listesinin kapali oldugunu
    # kanitlamaz.
    action: Mapped[str | None] = mapped_column(AsciiKeyType(16))
    price_target_action: Mapped[str | None] = mapped_column(String(16, collation="C"))
    # 0.0 GERCEK bir degerdir ("hedef yok"), NULL'a cevrilmez.
    current_price_target: Mapped[Decimal | None] = mapped_column(PriceType())
    prior_price_target: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystPriceTarget(Base):
    """current/low/high/mean/median. Tutarlilik kisiti YOKTUR: THYAO'da
    low(330) > current(294) olculdu; kaynak neyse o yazilir."""

    __tablename__ = "analyst_price_targets"
    __table_args__ = (
        Index("ix_analyst_price_targets_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    current: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    mean: Mapped[Decimal | None] = mapped_column(PriceType())
    median: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEstimate(Base):
    """earnings_estimate + revenue_estimate TEK tabloda; kolon setleri birebir
    ayni ve tek modulden geliyorlar.

    FactValueType (DECIMAL(38,10)) zorunlu: AYNI kolonda AAPL EPS 1.97656 ve
    THYAO revenue 1_285_436_390_920 bulunur.
    """

    __tablename__ = "analyst_estimates"
    __table_args__ = (
        Index("ix_analyst_estimates_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[EstimateMetric] = mapped_column(METRIC_ENUM, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    avg: Mapped[Decimal | None] = mapped_column(FactValueType())
    low: Mapped[Decimal | None] = mapped_column(FactValueType())
    high: Mapped[Decimal | None] = mapped_column(FactValueType())
    year_ago_value: Mapped[Decimal | None] = mapped_column(FactValueType())
    # Kaynakta float (1.0) ve NaN gelebiliyor
    number_of_analysts: Mapped[int | None] = mapped_column(Integer)
    growth: Mapped[Decimal | None] = mapped_column(PriceType())
    # yfinance'in ekledigi kolon; dokumantasyonda yok
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEpsTrend(Base):
    """EPS tahmininin zaman icindeki seyri. Kolon adi rakamla baslayamaz:
    7daysAgo -> days_ago_7."""

    __tablename__ = "analyst_eps_trend"
    __table_args__ = (
        Index("ix_analyst_eps_trend_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    current: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_7: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_30: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_60: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_90: Mapped[Decimal | None] = mapped_column(FactValueType())
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEpsRevision(Base):
    """Yukari/asagi revizyon SAYACLARI.

    Kaynak anahtarlari: upLast7days, upLast30days, downLast30days ve
    downLast7Days -- SONUNCUSUNDA D BUYUK (19/19 sembolde dogrulandi).
    Dokumantasyon dordunu de kucuk yaziyor; kucuk `d` ile okunursa kolon
    SESSIZCE hep NULL kalir.
    """

    __tablename__ = "analyst_eps_revisions"
    __table_args__ = (
        Index("ix_analyst_eps_revisions_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    up_last_7d: Mapped[int | None] = mapped_column(Integer)
    up_last_30d: Mapped[int | None] = mapped_column(Integer)
    down_last_7d: Mapped[int | None] = mapped_column(Integer)
    down_last_30d: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystGrowthEstimate(Base):
    """Buyume tahminleri; period 0q/+1q/0y/+1y/LTG.

    industry_trend ve sector_trend 19 sembolde HIC gelmedi ama yfinance
    modulleri acikca istiyor (industryTrend,sectorTrend,indexTrend); uc geri
    acildiginda migration gerekmesin diye kolonlar simdiden acilir.

    index_trend TUM sembollerde AYNIDIR (piyasa endeksi trendi); sembol basina
    denormalize saklanmasi bilinclidir -- tek kolon icin ayri bir piyasa
    tablosu karsiligi olmayan bir soyutlama olurdu.
    """

    __tablename__ = "analyst_growth_estimates"
    __table_args__ = (
        Index("ix_analyst_growth_estimates_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    stock_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    index_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    industry_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    sector_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class EarningsHistoryRow(Base):
    """Gerceklesen vs tahmin EPS. AS-OF DEGIL: kaynak ceyrek sonunu veriyor.

    quarter_end tz-naive Timestamp'ten .date() ile alinir; TZ DONUSUMU
    YAPILMAZ -- mali ceyrek takvimsel bir etikettir, bir an degil.
    """

    __tablename__ = "earnings_history"
    __table_args__ = (
        Index("ix_earnings_history_quarter", "quarter_end"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    quarter_end: Mapped[date] = mapped_column(Date, primary_key=True)
    eps_actual: Mapped[Decimal | None] = mapped_column(PriceType())
    eps_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    eps_difference: Mapped[Decimal | None] = mapped_column(PriceType())
    surprise_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
