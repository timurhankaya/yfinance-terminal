"""Shared DeclarativeBase and column type factories."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import VARCHAR, ForeignKey, MetaData, Numeric, Text, cast
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column
from sqlalchemy.types import UserDefinedType

# Constraint names must be deterministic. Unnamed constraints get a fresh
# name from Alembic on every run, so `alembic revision --autogenerate`
# reports a phantom diff forever.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- string types ----------------------------------------------------------
#
# Every string column uses COLLATE "C": byte-ordered, case-sensitive, and
# index-usable for LIKE without text_pattern_ops. Case-insensitive matching,
# where the domain needs it, happens on the write path instead.

# Shared width for symbols and symbol-like keys. It must be a single
# constant: audit rows write the same value into `sync_run_items.symbol`,
# and a wider key overflows there -- after the data is written, i.e. at
# the latest possible moment.
SYMBOL_LENGTH = 32


def SymbolType() -> VARCHAR:  # noqa: N802
    """Symbol key. Case-sensitive: 'AAPL' and 'aapl' must not collide.

    FK columns must match the parent's collation. PostgreSQL does not
    enforce that, so a drift is silent; test_schema_invariants guards it.
    """
    return VARCHAR(SYMBOL_LENGTH, collation="C")


def NewsIdType() -> VARCHAR:  # noqa: N802
    """News ids are fixed 36-character UUIDs."""
    return VARCHAR(36, collation="C")


def HashType() -> VARCHAR:  # noqa: N802
    """SHA-256 hex."""
    return VARCHAR(64, collation="C")


def PersonNameType() -> VARCHAR:  # noqa: N802
    """'Tim Cook' must not equal 'TIM COOK'."""
    return VARCHAR(255, collation="C")


def ShortHashType() -> VARCHAR:  # noqa: N802
    """First 16 hex digits of a SHA-256, used as a PK component.

    VARCHAR rather than CHAR: CHAR pads with spaces and would silently
    merge keys that differ only in trailing whitespace.
    """
    return VARCHAR(16, collation="C")


def BarIntervalType() -> VARCHAR:  # noqa: N802
    """Bar interval code: '1m', '5m', '15m', '60m', '1wk', '1mo'.

    Not an ENUM: the valid set lives in BAR_INTERVALS. The column is named
    `bar_interval`, not `interval`: INTERVAL is a PostgreSQL type name and
    the view definitions and rescale UPDATE are raw strings."""
    return VARCHAR(4, collation="C")


def RegionType() -> VARCHAR:  # noqa: N802
    """Market region code (US, EUROPE, CRYPTOCURRENCIES...).

    All three tables share this type; a width mismatch would break a
    future FK or JOIN.
    """
    return VARCHAR(16, collation="C")


def KeyTextType(length: int) -> VARCHAR:  # noqa: N802
    """Free text that is part of a primary key."""
    return VARCHAR(length, collation="C")


def AsciiKeyType(length: int) -> VARCHAR:  # noqa: N802
    """ASCII code field in a primary key (filing_type, action, board_code)."""
    return VARCHAR(length, collation="C")


def ProxyLabelType() -> VARCHAR:  # noqa: N802
    """proxies.label and sync_run_items.proxy_label. 'eu-1' != 'EU-1'."""
    return VARCHAR(64, collation="C")


def HostType() -> VARCHAR:  # noqa: N802
    """Proxy hostname or IP.

    Hostnames are case-insensitive (RFC 4343), and uq_proxies_endpoint
    relies on that; normalisation happens on write (`.lower()`)."""
    return VARCHAR(255, collation="C")


# --- numeric and timestamp types -------------------------------------------

PRICE_PRECISION = 28
PRICE_SCALE = 12
BIG_PRECISION = 38
FACT_PRECISION = 38
FACT_SCALE = 10


def PriceType() -> Numeric[Decimal]:  # noqa: N802
    """Prices and ratios. Verified to round-trip without loss."""
    return Numeric(PRICE_PRECISION, PRICE_SCALE, asdecimal=True)


def FactValueType() -> Numeric[Decimal]:  # noqa: N802
    """Financial statement line item.

    One column holds both huge totals and small ratios, so scale matters.
    PostgreSQL rounds excess decimals silently, so rounding is done
    explicitly in Python with quantize; an overflowing integer part raises."""
    return Numeric(FACT_PRECISION, FACT_SCALE, asdecimal=True)


def BigNumType() -> Numeric[Decimal]:  # noqa: N802
    """marketCap / totalRevenue / enterpriseValue.

    NUMERIC(28,12) would overflow on these.
    """
    return Numeric(BIG_PRECISION, 0, asdecimal=True)


def TsType() -> TIMESTAMP:  # noqa: N802
    """All timestamps are TIMESTAMP(6) WITH TIME ZONE.

    The dialect type is required: generic sqlalchemy.TIMESTAMP rejects
    `precision`. Six digits are mandatory: ticker_info_history is keyed on
    (symbol, fetched_at), and PostgreSQL rounds the fraction, not truncates."""
    return TIMESTAMP(timezone=True, precision=6)


def RawJsonType() -> Text:  # noqa: N802
    """raw_json is TEXT, never JSON/JSONB.

    jsonb reorders keys and normalises numbers, and both json types reject
    NaN; content_hash depends on the byte-faithful body."""
    return Text()


class Xid8Type(UserDefinedType[int]):
    """PostgreSQL's 64-bit transaction id, as a column type (not `xid`,
    which wraps). PostgreSQL has no implicit `bigint -> xid8` cast, so
    `bind_expression` casts every parameter; psycopg 3 has no loader for
    it, so the processors convert between text and int on the Python side
    and the relay can order `(xid, id)` numerically."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "xid8"

    def bind_expression(self, bindvalue: Any) -> Any:
        return cast(bindvalue, self)

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: int | None) -> str | None:
            return None if value is None else str(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> int | None:
            return None if value is None else int(value)

        return process


def symbol_fk_column(**kwargs: Any) -> MappedColumn[str]:
    """Symbol column carrying an FK to symbols.symbol.

    ON UPDATE CASCADE ON DELETE RESTRICT enforces the soft-delete policy
    in the database, so a single DELETE cannot drop 40 years of history.
    """
    return mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        **kwargs,
    )
