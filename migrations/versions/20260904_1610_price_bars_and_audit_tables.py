"""price_bars + intraday_scope + bar_gaps + bar_rescales (PB S5)

price_bars PARTITION'LIDIR ve bu yuzden FK TASIMAZ: MySQL 8 partition'li
InnoDB tablosunda foreign key desteklemez (ERROR 1506). Partition DDL'ini
Alembic autogenerate EDEMEZ; models.price_bars_partition_ddl() sabitinden
op.execute() ile uygulanir - ayni sabiti tests/conftest.py da kullanir,
aksi halde testler PARTITION'SIZ bir tabloya karsi kosardi.

Revision ID: a1c3e7d92f04
Revises: 3f5c81ae90d4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from yfin.models import (
    V_PRICE_BARS_REGULAR_CREATE,
    V_PRICE_BARS_REGULAR_DROP,
    price_bars_partition_ddl,
)

revision: str = "a1c3e7d92f04"
down_revision: str | None = "3f5c81ae90d4"
branch_labels: str | None = None
depends_on: str | None = None

_SYMBOL = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
_INTERVAL = mysql.VARCHAR(4, charset="ascii", collation="ascii_bin")
_REASON = mysql.VARCHAR(24, charset="ascii", collation="ascii_bin")
_TS = mysql.DATETIME(fsp=6)
_PRICE = sa.Numeric(28, 12)

_TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "price_bars",
        sa.Column("symbol", _SYMBOL, nullable=False),
        sa.Column("bar_interval", _INTERVAL, nullable=False),
        sa.Column("ts_utc", _TS, nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("open", _PRICE, nullable=True),
        sa.Column("high", _PRICE, nullable=True),
        sa.Column("low", _PRICE, nullable=True),
        sa.Column("close", _PRICE, nullable=False),
        sa.Column("volume", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("is_extended", sa.Boolean(), server_default="0", nullable=False),
        # ts_utc PK'nin PARCASI OLMAK ZORUNDA: MySQL partition anahtari
        # her unique key'in icinde bulunmalidir (ERROR 1503).
        sa.PrimaryKeyConstraint("symbol", "bar_interval", "ts_utc"),
        **_TABLE_KW,
    )
    op.create_index("ix_price_bars_local_date", "price_bars", ["local_date"], unique=False)
    # Partition'lama create_table'dan SONRA gelir; MAXVALUE bolumu YOKTUR
    # (gerekcesi price_bars_partition_ddl docstring'inde).
    op.execute(price_bars_partition_ddl())

    op.create_table(
        "intraday_scope",
        sa.Column("symbol", _SYMBOL, nullable=False),
        sa.Column("bar_interval", _INTERVAL, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("added_at", _TS, nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(
            ["symbol"], ["symbols.symbol"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("symbol", "bar_interval"),
        **_TABLE_KW,
    )

    op.create_table(
        "bar_gaps",
        sa.Column("symbol", _SYMBOL, nullable=False),
        sa.Column("bar_interval", _INTERVAL, nullable=False),
        sa.Column("gap_start_utc", _TS, nullable=False),
        sa.Column("gap_end_utc", _TS, nullable=False),
        sa.Column("detected_at", _TS, nullable=False),
        sa.Column("reason", _REASON, nullable=False),
        sa.Column("resolved_at", _TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["symbol"], ["symbols.symbol"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("symbol", "bar_interval", "gap_start_utc"),
        **_TABLE_KW,
    )
    op.create_index("ix_bar_gaps_detected_at", "bar_gaps", ["detected_at"], unique=False)

    op.create_table(
        "bar_rescales",
        sa.Column("symbol", _SYMBOL, nullable=False),
        sa.Column("split_date", sa.Date(), nullable=False),
        sa.Column("ratio", _PRICE, nullable=False),
        sa.Column("applied_at", _TS, nullable=False),
        sa.Column("rows_affected", mysql.BIGINT(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["symbol"], ["symbols.symbol"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        # splits tablosunun PK'siyla birebir ayni: "bu split uygulandi mi"
        # sorusu tek anahtar aramasidir.
        sa.PrimaryKeyConstraint("symbol", "split_date"),
        **_TABLE_KW,
    )

    op.execute(V_PRICE_BARS_REGULAR_CREATE)


def downgrade() -> None:
    op.execute(V_PRICE_BARS_REGULAR_DROP)
    op.drop_table("bar_rescales")
    op.drop_index("ix_bar_gaps_detected_at", table_name="bar_gaps")
    op.drop_table("bar_gaps")
    op.drop_table("intraday_scope")
    op.drop_index("ix_price_bars_local_date", table_name="price_bars")
    op.drop_table("price_bars")
