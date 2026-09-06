"""symbols table -- an infrastructure dataset, run first on every invocation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import Base, SymbolType, TsType


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_exchange", "exchange"),
        Index("ix_symbols_isin", "isin"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # isin is not UNIQUE: the same ISIN can be listed on different exchanges.
    isin: Mapped[str | None] = mapped_column(String(16, collation="C"))
    quote_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    full_exchange_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    currency: Mapped[str | None] = mapped_column(String(32, collation="C"))
    timezone: Mapped[str | None] = mapped_column(String(64, collation="C"))
    short_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    long_name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    first_trade_date: Mapped[datetime | None] = mapped_column(TsType())

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Who brought the symbol into the universe. Discovery paths (`search`,
    # `lookup`, `screener`) write new symbols with `is_active=0`;
    # activation is manual (`yfin symbols activate --discovered-by ...`).
    #
    # This column and `discovered_at` are kept out of discovery writes'
    # `update_columns` scope, along with `is_active` and `unknown_streak`.
    # Otherwise a symbol an operator manually activated would silently
    # flip back to `is_active=0` the next time discovery saw it again, and
    # `yfin sync` would stop pulling it. Same "written only on INSERT"
    # rule as `first_seen_at`.
    discovered_by: Mapped[str] = mapped_column(
        String(16, collation="C"), nullable=False, server_default="manual"
    )
    discovered_at: Mapped[datetime | None] = mapped_column(TsType())
    # Consecutive unknown_symbol counter; past the threshold, is_active=0.
    unknown_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(TsType())

    # `func.now()`, not `func.now(6)`: PostgreSQL's `now()` takes no
    # argument, and `now(6)` raises "function now(integer) does not exist"
    # (measured, during migration). Precision comes from the column type
    # (TsType = TIMESTAMP(6) WITH TIME ZONE), not the function.
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(),
        nullable=False,
        server_default=func.now(),
        # `server_onupdate` generates no DDL on PostgreSQL (there is no ON
        # UPDATE column clause); it only tells SQLAlchemy the value may
        # change server-side.
        server_onupdate=func.now(),
    )
