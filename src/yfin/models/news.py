"""news ve news_symbols (S5.2, S5.5)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    Base,
    NewsIdType,
    RawJsonType,
    SymbolType,
    TsType,
)


class News(Base):
    """symbols'a FK ile bagli DEGILDIR; baglanti news_symbols uzerindendir."""

    __tablename__ = "news"
    __table_args__ = (Index("ix_news_pub_date", "pub_date"),)

    news_id: Mapped[str] = mapped_column(NewsIdType(), primary_key=True)
    title: Mapped[str] = mapped_column(String(512, collation="C"), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    pub_date: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    display_time: Mapped[datetime | None] = mapped_column(TsType())
    provider_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    provider_url: Mapped[str | None] = mapped_column(String(255, collation="C"))
    provider_source_id: Mapped[str | None] = mapped_column(String(64, collation="C"))
    canonical_url: Mapped[str | None] = mapped_column(Text)
    click_through_url: Mapped[str | None] = mapped_column(Text)
    # tag="original" cozunurlugu; diger cozunurlukler raw_json'da kalir
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    thumbnail_width: Mapped[int | None] = mapped_column(Integer)
    thumbnail_height: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class NewsSymbol(Base):
    """symbol uzerinde FK YOKTUR (S5.5).

    Kaynakta evren disi semboller geliyor (bir AAPL haberinde 005930.KS,
    ^GSPC, IRTC). FK olsaydi sembol basina tek transaction geregi TUM
    sembolun verisi rollback olurdu.
    """

    __tablename__ = "news_symbols"
    __table_args__ = (
        # FK olmadigi icin bu index ACIKCA tanimlanir (S5.6)
        Index("ix_news_symbols_symbol", "symbol"),
    )

    news_id: Mapped[str] = mapped_column(
        NewsIdType(), ForeignKey("news.news_id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    # sembol symbols tablosunda var mi
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
