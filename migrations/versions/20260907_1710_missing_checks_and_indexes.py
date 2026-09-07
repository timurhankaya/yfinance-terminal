"""Create the constraints and indexes the schema declares but no migration made.

The models declare 47 CHECK constraints. Twelve reached the database; the
other 39 never did, and the reason is that alembic's autogenerate does not
compare CHECK constraints at all -- so `alembic check` stayed green while a
migrated database accepted negative volumes, a share count below zero, a
holding rank outside 0-255 and a proxy port outside 1-65535.

The gap was invisible from the test suite too, in the direction that hides
it: `tests/conftest.py` builds its schema with `create_all`, which DOES
emit them. Every constraint was enforced in tests and absent in production.

Three indexes have the same shape for a different reason. `migrations/env.py`
excludes them from autogenerate because alembic cannot read an expression
index back to compare it, and says they are "managed by hand" -- but no hand
ever created them, so `ticker_info_history` and `ticker_fast_info_history`
were answering "the latest snapshot for this symbol" with a sequential scan.

Adding them validated rather than NOT VALID is deliberate. A row that
violates one of these is wrong, and a migration that reports it is the
cheapest place to find out; leaving it unvalidated would carry the same
silence forward under a different name.

Revision ID: 8f3b1c07a41d
Revises: 62259f494923
Create Date: 2026-09-07 17:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f3b1c07a41d"
down_revision: str | None = "62259f494923"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (table, constraint name, condition). Generated from the models at the
#: time this migration was written and then frozen, the way a migration
#: has to be: it describes one step, not the current schema.
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("api_clients", "ck_api_clients_epoch_nonneg", '"auth_epoch" >= 0'),
    ("api_usage_daily", "ck_api_usage_daily_count_nonneg", '"request_count" >= 0'),
    ("bar_rescales", "ck_bar_rescales_rows_affected_nonneg", '"rows_affected" >= 0'),
    (
        "domain_metrics",
        "ck_domain_metrics_employee_count_nonneg",
        '"employee_count" >= 0',
    ),
    (
        "fund_top_holdings",
        "ck_fund_top_holdings_holding_rank_range",
        '"holding_rank" BETWEEN 0 AND 255',
    ),
    (
        "history_metadata",
        "ck_history_metadata_regular_market_volume_nonneg",
        '"regular_market_volume" >= 0',
    ),
    ("periodic_bars", "ck_periodic_bars_volume_nonneg", '"volume" >= 0'),
    ("price_bars", "ck_price_bars_volume_nonneg", '"volume" >= 0'),
    ("price_history", "ck_price_history_volume_nonneg", '"volume" >= 0'),
    ("proxies", "ck_proxies_port_range", '"port" BETWEEN 1 AND 65535'),
    ("screen_quotes", "ck_screen_quotes_ask_size_nonneg", '"ask_size" >= 0'),
    (
        "screen_quotes",
        "ck_screen_quotes_average_daily_volume_10day_nonneg",
        '"average_daily_volume_10day" >= 0',
    ),
    (
        "screen_quotes",
        "ck_screen_quotes_average_daily_volume_3month_nonneg",
        '"average_daily_volume_3month" >= 0',
    ),
    ("screen_quotes", "ck_screen_quotes_bid_size_nonneg", '"bid_size" >= 0'),
    (
        "screen_quotes",
        "ck_screen_quotes_regular_market_volume_nonneg",
        '"regular_market_volume" >= 0',
    ),
    ("shares_full", "ck_shares_full_shares_nonneg", '"shares" >= 0'),
    ("sync_run_items", "ck_sync_run_items_proxy_id_nonneg", '"proxy_id" >= 0'),
    (
        "ticker_fast_info_history",
        "ck_ticker_fast_info_history_last_volume_nonneg",
        '"last_volume" >= 0',
    ),
    (
        "ticker_fast_info_history",
        "ck_ticker_fast_info_history_ten_day_average_volume_nonneg",
        '"ten_day_average_volume" >= 0',
    ),
    (
        "ticker_fast_info_history",
        "ck_ticker_fast_info_history_three_month_average_volume_nonneg",
        '"three_month_average_volume" >= 0',
    ),
    ("ticker_fast_info", "ck_ticker_fast_info_last_volume_nonneg", '"last_volume" >= 0'),
    (
        "ticker_fast_info",
        "ck_ticker_fast_info_ten_day_average_volume_nonneg",
        '"ten_day_average_volume" >= 0',
    ),
    (
        "ticker_fast_info",
        "ck_ticker_fast_info_three_month_average_volume_nonneg",
        '"three_month_average_volume" >= 0',
    ),
    ("ticker_info", "ck_ticker_info_ask_size_nonneg", '"ask_size" >= 0'),
    (
        "ticker_info",
        "ck_ticker_info_average_daily_volume_10day_nonneg",
        '"average_daily_volume_10day" >= 0',
    ),
    (
        "ticker_info",
        "ck_ticker_info_average_daily_volume_3month_nonneg",
        '"average_daily_volume_3month" >= 0',
    ),
    (
        "ticker_info",
        "ck_ticker_info_average_volume_10days_nonneg",
        '"average_volume_10days" >= 0',
    ),
    ("ticker_info", "ck_ticker_info_average_volume_nonneg", '"average_volume" >= 0'),
    ("ticker_info", "ck_ticker_info_bid_size_nonneg", '"bid_size" >= 0'),
    (
        "ticker_info",
        "ck_ticker_info_regular_market_volume_nonneg",
        '"regular_market_volume" >= 0',
    ),
    ("ticker_info", "ck_ticker_info_volume_nonneg", '"volume" >= 0'),
    ("ticker_info_history", "ck_ticker_info_history_ask_size_nonneg", '"ask_size" >= 0'),
    (
        "ticker_info_history",
        "ck_ticker_info_history_average_daily_volume_10day_nonneg",
        '"average_daily_volume_10day" >= 0',
    ),
    (
        "ticker_info_history",
        "ck_ticker_info_history_average_daily_volume_3month_nonneg",
        '"average_daily_volume_3month" >= 0',
    ),
    (
        "ticker_info_history",
        "ck_ticker_info_history_average_volume_10days_nonneg",
        '"average_volume_10days" >= 0',
    ),
    (
        "ticker_info_history",
        "ck_ticker_info_history_average_volume_nonneg",
        '"average_volume" >= 0',
    ),
    ("ticker_info_history", "ck_ticker_info_history_bid_size_nonneg", '"bid_size" >= 0'),
    (
        "ticker_info_history",
        "ck_ticker_info_history_regular_market_volume_nonneg",
        '"regular_market_volume" >= 0',
    ),
    ("ticker_info_history", "ck_ticker_info_history_volume_nonneg", '"volume" >= 0'),
)

#: Expression indexes, which autogenerate cannot compare and therefore
#: never proposed. Written as SQL because `op.create_index` cannot express
#: a DESC ordering on a raw column reference portably.
INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ix_ticker_info_history_symbol_fetched",
        'CREATE INDEX IF NOT EXISTS ix_ticker_info_history_symbol_fetched '
        'ON ticker_info_history (symbol, fetched_at DESC)',
    ),
    (
        "ix_ticker_fast_info_history_symbol_fetched",
        'CREATE INDEX IF NOT EXISTS ix_ticker_fast_info_history_symbol_fetched '
        'ON ticker_fast_info_history (symbol, fetched_at DESC)',
    ),
    (
        "ix_sec_filings_symbol_date",
        'CREATE INDEX IF NOT EXISTS ix_sec_filings_symbol_date '
        'ON sec_filings (symbol, filing_date DESC)',
    ),
)


def upgrade() -> None:
    connection = op.get_bind()
    for table, name, condition in CHECKS:
        # IF NOT EXISTS is not available for ADD CONSTRAINT, and a database
        # created by `create_all` rather than by migration already has
        # these. Checking first keeps this runnable against both.
        exists = connection.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name "
                "AND conrelid = to_regclass(:table)"
            ),
            {"name": name, "table": table},
        ).scalar()
        if not exists:
            op.create_check_constraint(name, table, sa.text(condition))

    for _, statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for name, _ in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for table, name, _ in reversed(CHECKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
