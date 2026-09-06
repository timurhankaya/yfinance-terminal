"""Shared DeclarativeBase and column type factories."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import VARCHAR, ForeignKey, MetaData, Numeric, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

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
# usable by an index for LIKE without text_pattern_ops (measured: "C"
# column gets an Index Scan where an en_US.utf8 column falls back to Seq
# Scan). Case-insensitive matching, where the domain needs it, happens on
# the write path instead -- see datasets/symbols.py and
# scripts/seed_proxies.py.

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

    Not an ENUM: the valid set lives in BAR_INTERVALS (models/bars.py) and
    duplicating it in the schema creates a second source of truth that can
    drift.

    The column is named `bar_interval`, not `interval`: INTERVAL is a type
    name in PostgreSQL. SQLAlchemy quotes its own SQL, but view
    definitions and the rescale UPDATE are raw strings.
    """
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
    relies on that. The normalisation happens on write (`.lower()`), not
    in the collation.
    """
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

    One column holds both 1.06e14 (7203.T total assets) and 0.156
    (TaxRateForCalcs), so NUMERIC(38,0) would destroy the ratios.

    PostgreSQL rounds the 11th decimal silently (measured: numeric(5,2)
    given 1.239 stores 1.24, no warning), so rounding is done explicitly
    in Python with quantize. An overflowing integer part does raise.
    """
    return Numeric(FACT_PRECISION, FACT_SCALE, asdecimal=True)


def BigNumType() -> Numeric[Decimal]:  # noqa: N802
    """marketCap / totalRevenue / enterpriseValue.

    NUMERIC(28,12) would overflow on these.
    """
    return Numeric(BIG_PRECISION, 0, asdecimal=True)


def TsType() -> TIMESTAMP:  # noqa: N802
    """All timestamps are TIMESTAMP(6) WITH TIME ZONE.

    The dialect type is required: generic sqlalchemy.TIMESTAMP rejects
    `precision`.

    Six digits are mandatory. ticker_info_history is keyed on
    (symbol, fetched_at), so second precision would collide within the
    same second -- and PostgreSQL rounds the fraction rather than
    truncating it.
    """
    return TIMESTAMP(timezone=True, precision=6)


def RawJsonType() -> Text:  # noqa: N802
    """raw_json is TEXT, never JSON/JSONB.

    jsonb reorders keys (so content_hash could not be recomputed),
    rejects NaN bodies, and normalises numbers (0.001870 -> 0.00187).
    `json` still validates syntax and rejects NaN. TEXT is byte-faithful,
    which content_hash depends on.
    """
    return Text()


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
