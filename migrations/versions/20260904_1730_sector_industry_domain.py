"""Sektor / endustri (domain) tablolari (SI S11.1)

8 tablo (7 veri + 1 kapi), `sync_runs.scope` ENUM'una 1 deger,
`sync_run_items`'a 1 kolon. `symbols`'a SEMA DEGISIKLIGI YOKTUR -- 156 satir
`domain_taxonomy` tarafindan veri olarak yazilir.

YENI DESEN UYARISI: `domains`in CHECK kisiti kod tabanindaki ILK
`CheckConstraint`'tir (MySQL 8.0.16+ destekliyor, 8.3.0'da dogrulandi).

Revision ID: c5b1d0e6a942
Revises: e7f2a94c1b83
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, LONGTEXT, MEDIUMTEXT, VARCHAR

revision: str = "c5b1d0e6a942"
down_revision: str | None = "e7f2a94c1b83"
branch_labels: str | None = None
depends_on: str | None = None

_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _ascii_key(length: int) -> VARCHAR:
    return VARCHAR(length, charset="ascii", collation="ascii_bin")


def _symbol() -> VARCHAR:
    return VARCHAR(32, charset="ascii", collation="ascii_bin")


def _region() -> VARCHAR:
    return VARCHAR(16, charset="ascii", collation="ascii_bin")


def _ts() -> DATETIME:
    return DATETIME(fsp=6)


_DOMAIN_KEY = 48
_REPORT_ID = 64

# Yeni deger ENUM'un SONUNA eklendigi icin ALGORITHM=INSTANT
_SCOPE_NEW = "ENUM('symbols','market','domain')"
_SCOPE_OLD = "ENUM('symbols','market')"


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("domain_type", sa.Enum("sector", "industry", name="domain_type"),
                  nullable=False),
        sa.Column("symbol", _symbol(), nullable=False),
        sa.Column("parent_key", _ascii_key(_DOMAIN_KEY), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("message_board_id", sa.String(32), nullable=True),
        sa.Column("first_seen_at", _ts(), nullable=False),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.UniqueConstraint("symbol", name="uq_domains_symbol"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["symbols.symbol"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        # SELF-FK. ON DELETE RESTRICT burada da gecerlidir: bir sektoru
        # silmek 145 endustriyi oksuz birakamaz.
        sa.ForeignKeyConstraint(
            # ON UPDATE RESTRICT ZORUNLU: `ck_domains_parent` CHECK'i
            # `parent_key`e bakiyor ve MySQL 8.3.0 CASCADE ile ERROR 3823
            # veriyor ("Column 'parent_key' cannot be used in a check
            # constraint ... needed in a foreign key constraint
            # referential action").
            ["parent_key"], ["domains.domain_key"], onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "domain_type = 'sector' OR parent_key IS NOT NULL", name="ck_domains_parent"
        ),
        **_ARGS,
    )
    op.create_index("ix_domains_type_parent", "domains", ["domain_type", "parent_key"])

    op.create_table(
        "domain_metrics",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("as_of_date", sa.Date(), primary_key=True),
        sa.Column("companies_count", sa.Integer(), nullable=True),
        sa.Column("industries_count", sa.Integer(), nullable=True),
        sa.Column("market_cap", sa.Numeric(38, 0), nullable=True),
        sa.Column("market_weight", sa.Numeric(28, 12), nullable=True),
        sa.Column("employee_count", BIGINT(unsigned=True), nullable=True),
        sa.Column("ytd_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("reg_market_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("one_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("three_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("five_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("benchmark_name", sa.String(64), nullable=True),
        sa.Column("benchmark_ytd_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("benchmark_reg_market_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("benchmark_one_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("benchmark_three_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("benchmark_five_year_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("raw_json", LONGTEXT(), nullable=False),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        **_ARGS,
    )
    op.create_index("ix_domain_metrics_date", "domain_metrics", ["as_of_date"])

    op.create_table(
        "domain_top_companies",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("region", _region(), primary_key=True),
        sa.Column("as_of_date", sa.Date(), primary_key=True),
        # FK YOKTUR (SI S2): evren disi sembol tek turun transaction'ini
        # dusururdu. `is_known` bayragi DB'den doldurulur.
        sa.Column("symbol", _symbol(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("rating", sa.String(32), nullable=True),
        sa.Column("market_weight", sa.Numeric(28, 12), nullable=True),
        sa.Column("market_cap", sa.Numeric(38, 0), nullable=True),
        sa.Column("last_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("target_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("ytd_return", sa.Numeric(28, 12), nullable=True),
        sa.Column("reg_market_change_pct", sa.Numeric(28, 12), nullable=True),
        sa.Column("is_known", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        **_ARGS,
    )
    op.create_index("ix_domain_top_companies_symbol", "domain_top_companies", ["symbol"])

    op.create_table(
        "domain_top_funds",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("region", _region(), primary_key=True),
        sa.Column("as_of_date", sa.Date(), primary_key=True),
        sa.Column(
            "fund_type",
            sa.Enum("etf", "mutual_fund", name="domain_fund_type"),
            primary_key=True,
        ),
        sa.Column("symbol", _symbol(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("net_assets", sa.Numeric(38, 0), nullable=True),
        sa.Column("expense_ratio", sa.Numeric(28, 12), nullable=True),
        sa.Column("last_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("ytd_return", sa.Numeric(28, 12), nullable=True),
        sa.Column("is_known", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        **_ARGS,
    )

    op.create_table(
        "domain_top_movers",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("region", _region(), primary_key=True),
        sa.Column("as_of_date", sa.Date(), primary_key=True),
        sa.Column(
            "rank_type",
            sa.Enum("performing", "growth", name="domain_rank_type"),
            primary_key=True,
        ),
        sa.Column("symbol", _symbol(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("ytd_return", sa.Numeric(28, 12), nullable=True),
        sa.Column("last_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("target_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("growth_estimate", sa.Numeric(28, 12), nullable=True),
        sa.Column("is_known", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        **_ARGS,
    )

    op.create_table(
        "domain_research_reports",
        sa.Column("report_id", _ascii_key(_REPORT_ID), primary_key=True),
        # PK'da DEGIL -- "en son gorulduğu gun". Kolonun VAR OLMASI zorunlu:
        # `AsOfGate` kapi satirinin `as_of_date`'ini `writes`'in ilk
        # satirindan okur.
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("report_type", sa.String(64), nullable=True),
        sa.Column("head_html", sa.String(255), nullable=True),
        # MEDIUMTEXT ZORUNLU: olculen max 23 570 karakter; TEXT 65 535
        # BAYT'tir ve utf8mb4'te tasabilir.
        sa.Column("report_title", MEDIUMTEXT(), nullable=True),
        sa.Column("target_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("target_price_status", sa.String(32), nullable=True),
        sa.Column("investment_rating", sa.String(32), nullable=True),
        sa.Column("report_ts_utc", _ts(), nullable=True),
        sa.Column("first_seen_at", _ts(), nullable=False),
        sa.Column("fetched_at", _ts(), nullable=False),
        **_ARGS,
    )
    op.create_index(
        "ix_domain_research_reports_date", "domain_research_reports", ["as_of_date"]
    )

    op.create_table(
        "domain_report_links",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("as_of_date", sa.Date(), primary_key=True),
        sa.Column("report_id", _ascii_key(_REPORT_ID), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["domain_research_reports.report_id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        **_ARGS,
    )

    op.create_table(
        "domain_asof_state",
        sa.Column("domain_key", _ascii_key(_DOMAIN_KEY), primary_key=True),
        sa.Column("dataset", _ascii_key(32), primary_key=True),
        sa.Column("region", _region(), primary_key=True),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column(
            "content_hash",
            VARCHAR(64, charset="ascii", collation="ascii_general_ci"),
            nullable=False,
        ),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", _ts(), nullable=False),
        sa.Column("fetched_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_key"], ["domains.domain_key"], onupdate="CASCADE", ondelete="RESTRICT"
        ),
        **_ARGS,
    )
    op.create_index(
        "ix_domain_asof_dataset_date", "domain_asof_state", ["dataset", "as_of_date"]
    )

    op.execute(
        f"ALTER TABLE sync_runs MODIFY COLUMN scope {_SCOPE_NEW} "
        "NOT NULL DEFAULT 'symbols'"
    )
    op.execute(
        "ALTER TABLE sync_run_items ADD COLUMN region "
        "VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NULL"
    )


def downgrade() -> None:
    # ENUM DARALTMASINDAN ONCE. Kurulu desen bunu zorunlu kiliyor
    # (20260904_1640_item_status_out_of_scope.py:36-40); yapilmazsa MySQL o
    # satirlarin degerini SESSIZCE '' yapar.
    op.execute("UPDATE sync_runs SET scope = 'symbols' WHERE scope = 'domain'")
    op.execute(
        f"ALTER TABLE sync_runs MODIFY COLUMN scope {_SCOPE_OLD} NOT NULL DEFAULT 'symbols'"
    )
    op.drop_column("sync_run_items", "region")

    # FK ters sirasi
    op.drop_table("domain_asof_state")
    op.drop_table("domain_report_links")
    op.drop_table("domain_research_reports")
    op.drop_table("domain_top_movers")
    op.drop_table("domain_top_funds")
    op.drop_table("domain_top_companies")
    op.drop_table("domain_metrics")
    op.drop_table("domains")
    # `symbols`taki 156 satir SILINMEZ: ON DELETE RESTRICT tasiyan tablolar
    # onlara bagli olabilir ve silme `yfin symbols purge --force`
    # kullanicisinin kararidir.
