"""news and news_symbols tables."""

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
    """No FK to symbols; the link goes through news_symbols."""

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
    # The tag="original" resolution; other resolutions stay in raw_json.
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    thumbnail_width: Mapped[int | None] = mapped_column(Integer)
    thumbnail_height: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class NewsSymbol(Base):
    """No FK on `symbol`.

    Source data includes symbols outside the universe (an AAPL story
    tagging 005930.KS, ^GSPC, IRTC). An FK would roll back an entire
    article's data over one unknown symbol, since all its symbols write
    in one transaction.
    """

    __tablename__ = "news_symbols"
    __table_args__ = (
        # Defined explicitly since there is no FK to derive it from.
        Index("ix_news_symbols_symbol", "symbol"),
    )

    news_id: Mapped[str] = mapped_column(
        NewsIdType(), ForeignKey("news.news_id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    # Whether the symbol exists in the symbols table.
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
