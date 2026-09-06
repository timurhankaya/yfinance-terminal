"""proxy havuzu, shard alanlari ve history repair kolonu

ENUM degisikligi (sync_run_items.status) Alembic autogenerate tarafindan
YAKALANMAZ; bu yuzden elle yazilmistir.

Revision ID: c7a1e4f20b93
Revises: b48f9eedda67
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "c7a1e4f20b93"
down_revision: str | None = "b48f9eedda67"
branch_labels: str | None = None
depends_on: str | None = None

_ITEM_STATUS_NEW = "ENUM('ok','empty','skipped','failed','unknown_symbol','not_attempted')"
_ITEM_STATUS_OLD = "ENUM('ok','empty','skipped','failed','unknown_symbol')"

_LABEL = mysql.VARCHAR(64, charset="ascii", collation="ascii_bin")
_HOST = mysql.VARCHAR(255, charset="ascii", collation="ascii_general_ci")
_TS = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "proxies",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("label", _LABEL, nullable=False),
        sa.Column(
            "scheme",
            sa.Enum("http", "https", "socks5", "socks5h", name="proxyscheme"),
            nullable=False,
        ),
        sa.Column("host", _HOST, nullable=False),
        sa.Column("port", mysql.SMALLINT(unsigned=True), nullable=False),
        # NOT NULL DEFAULT '': MySQL UNIQUE, NULL iceren satirlarda
        # tekilligi zorlamaz; nullable birakilsaydi ayni proxy defalarca
        # eklenebilirdi.
        sa.Column("username", _LABEL, nullable=False, server_default=sa.text("''")),
        sa.Column("password_enc", mysql.VARBINARY(512), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "health",
            sa.Enum("unknown", "healthy", "cooldown", "dead", name="proxyhealth"),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("cooldown_until", _TS, nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_ok_at", _TS, nullable=True),
        sa.Column("last_error_at", _TS, nullable=True),
        sa.Column("last_checked_at", _TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")
        ),
        sa.Column(
            "updated_at",
            _TS,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label"),
        sa.UniqueConstraint("scheme", "host", "port", "username", name="uq_proxies_endpoint"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_proxies_eligibility", "proxies", ["is_enabled", "health"])

    op.add_column(
        "sync_runs",
        sa.Column("shard_count", mysql.SMALLINT(), nullable=False, server_default="1"),
    )

    op.add_column(
        "sync_run_items",
        sa.Column("shard_index", mysql.SMALLINT(), nullable=False, server_default="0"),
    )
    # FK YOKTUR: InnoDB her INSERT icin ebeveyn proxies satirina shared
    # lock alir ve bu, saglik flush'inin X-lock'uyla deadlock uretirdi.
    op.add_column(
        "sync_run_items", sa.Column("proxy_id", mysql.BIGINT(unsigned=True), nullable=True)
    )
    op.add_column("sync_run_items", sa.Column("proxy_label", _LABEL, nullable=True))
    op.create_index("ix_sync_run_items_proxy", "sync_run_items", ["proxy_id", "status"])
    op.execute(
        f"ALTER TABLE sync_run_items MODIFY COLUMN status {_ITEM_STATUS_NEW} NOT NULL"
    )

    op.add_column(
        "price_history",
        sa.Column("is_repaired", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("price_history", "is_repaired")
    op.execute(
        "UPDATE sync_run_items SET status = 'failed' WHERE status = 'not_attempted'"
    )
    op.execute(
        f"ALTER TABLE sync_run_items MODIFY COLUMN status {_ITEM_STATUS_OLD} NOT NULL"
    )
    op.drop_index("ix_sync_run_items_proxy", table_name="sync_run_items")
    op.drop_column("sync_run_items", "proxy_label")
    op.drop_column("sync_run_items", "proxy_id")
    op.drop_column("sync_run_items", "shard_index")
    op.drop_column("sync_runs", "shard_count")
    op.drop_index("ix_proxies_eligibility", table_name="proxies")
    op.drop_table("proxies")
