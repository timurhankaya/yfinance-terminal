"""Search & Lookup + Screener tablolari (SQ S12.1/Rev2)

On tablo + `symbols`a iki kolon.

`discovery_asof_state` AYRI BIR KAPI TABLOSUDUR ve bu tasarimin en kritik
sema karari odur (SQ K3a): mevcut `asof_state.symbol` kolonu
`symbol_fk_column` ile tanimlidir, yani `symbols.symbol`a
`ON DELETE RESTRICT` FK tasir. Serbest arama terimi (`"Turkish Airlines"`)
`symbols`ta YOKTUR; o kapiya yazilmaya calisilsaydi `ERROR 1452` alinirdi.
SI ayni duvara carpip `domain_asof_state`i acmisti.

`screens` SEED EDILMEZ. Kod tabaninin kendi deseni budur: SI'de de 156
domain satiri migration'la degil `domain_taxonomy` dataset'iyle veri
olarak yazilir. Migration'in uygulama kodunu import etmesi ayrica
kirilgandir -- `screens.py` yarin degistiginde bu revision'in gecmisteki
anlami degisirdi. Satirlari `screener` dataset'i her kosuda upsert eder
(`produces` listesinde `screens` vardir) ve `variants()` ekran kumesini
`screens.py`den alip YALNIZCA DB'de acikca kapatilmis olanlari eler --
boylece bos tabloyla bootstrap kilitlenmesi olusmaz.

`symbols.discovered_by` server_default='manual': mevcut satirlar geriye
donuk etiketlenir ve NOT NULL kisiti ilk calistirmada patlamaz
(ALGORITHM=INSTANT).

Revision ID: f3b8c1d47e29
Revises: d1a4f7c2e8b6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql
revision: str = "f3b8c1d47e29"
down_revision: str | None = "d1a4f7c2e8b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('discovery_asof_state',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('dataset', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('content_hash', mysql.VARCHAR(charset='ascii', collation='ascii_general_ci', length=64), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('first_seen_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('query_term', 'dataset'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_discovery_asof_dataset_date', 'discovery_asof_state', ['dataset', 'as_of_date'], unique=False)
    op.create_table('lookup_results',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('symbol', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('rank_index', mysql.SMALLINT(), nullable=False),
    sa.Column('source_rank', sa.Integer(), nullable=True),
    sa.Column('lookup_type', sa.String(length=24), nullable=True),
    sa.Column('quote_type', sa.String(length=32), nullable=True),
    sa.Column('exchange', sa.String(length=32), nullable=True),
    sa.Column('short_name', sa.String(length=128), nullable=True),
    sa.Column('industry_name', sa.String(length=128), nullable=True),
    sa.Column('industry_link', sa.Text(), nullable=True),
    sa.Column('fullday_price', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fullday_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fullday_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_price', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_percent_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('is_known', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('raw_json', mysql.LONGTEXT(), nullable=False),
    sa.PrimaryKeyConstraint('query_term', 'as_of_date', 'symbol'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_lookup_results_symbol', 'lookup_results', ['symbol'], unique=False)
    op.create_table('lookup_totals',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('lookup_type', sa.String(length=24), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('query_term', 'as_of_date', 'lookup_type'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('screen_members',
    sa.Column('screen_key', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('symbol', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('rank_index', sa.Integer(), nullable=False),
    sa.Column('is_known', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('screen_key', 'as_of_date', 'symbol'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_screen_members_symbol', 'screen_members', ['symbol'], unique=False)
    op.create_table('screen_quotes',
    sa.Column('symbol', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('quote_type', sa.String(length=32), nullable=True),
    sa.Column('type_disp', sa.String(length=64), nullable=True),
    sa.Column('short_name', sa.String(length=128), nullable=True),
    sa.Column('long_name', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=128), nullable=True),
    sa.Column('exchange', sa.String(length=32), nullable=True),
    sa.Column('full_exchange_name', sa.String(length=64), nullable=True),
    sa.Column('exchange_timezone_name', sa.String(length=64), nullable=True),
    sa.Column('exchange_timezone_short_name', sa.String(length=16), nullable=True),
    sa.Column('market', sa.String(length=32), nullable=True),
    sa.Column('region', sa.String(length=16), nullable=True),
    sa.Column('language', sa.String(length=16), nullable=True),
    sa.Column('market_state', sa.String(length=32), nullable=True),
    sa.Column('currency', sa.String(length=32), nullable=True),
    sa.Column('financial_currency', sa.String(length=32), nullable=True),
    sa.Column('quote_source_name', sa.String(length=64), nullable=True),
    sa.Column('message_board_id', sa.String(length=64), nullable=True),
    sa.Column('price_hint', sa.Integer(), nullable=True),
    sa.Column('source_interval', sa.Integer(), nullable=True),
    sa.Column('exchange_data_delayed_by', sa.Integer(), nullable=True),
    sa.Column('gmt_offset_milliseconds', sa.Integer(), nullable=True),
    sa.Column('average_analyst_rating', sa.String(length=32), nullable=True),
    sa.Column('regular_market_price', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_open', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_day_high', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_day_low', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_previous_close', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_day_range', sa.String(length=64), nullable=True),
    sa.Column('bid', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('ask', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('bid_size', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('ask_size', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('fifty_day_average', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('two_hundred_day_average', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_high', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_low', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_range', sa.String(length=64), nullable=True),
    sa.Column('fifty_two_week_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('regular_market_volume', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('average_daily_volume_10day', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('average_daily_volume_3month', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('market_cap', sa.Numeric(precision=38, scale=0), nullable=True),
    sa.Column('net_assets', sa.Numeric(precision=38, scale=0), nullable=True),
    sa.Column('shares_outstanding', sa.Numeric(precision=38, scale=0), nullable=True),
    sa.Column('implied_shares_outstanding', sa.Numeric(precision=38, scale=0), nullable=True),
    sa.Column('trailing_pe', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('forward_pe', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('price_to_book', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('book_value', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('eps_trailing_twelve_months', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('eps_current_year', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('eps_forward', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('price_eps_current_year', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('dividend_rate', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('dividend_yield', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('trailing_annual_dividend_rate', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('trailing_annual_dividend_yield', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('net_expense_ratio', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('ytd_return', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('tradeable', sa.Boolean(), nullable=True),
    sa.Column('triggerable', sa.Boolean(), nullable=True),
    sa.Column('crypto_tradeable', sa.Boolean(), nullable=True),
    sa.Column('esg_populated', sa.Boolean(), nullable=True),
    sa.Column('has_pre_post_market_data', sa.Boolean(), nullable=True),
    sa.Column('is_earnings_date_estimate', sa.Boolean(), nullable=True),
    sa.Column('first_trade_date', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('regular_market_time', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('dividend_date', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('earnings_timestamp', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('earnings_timestamp_start', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('earnings_timestamp_end', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('earnings_call_timestamp_start', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('earnings_call_timestamp_end', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('fullday_price', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fullday_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fullday_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_day_average_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_day_average_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_high_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_high_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_low_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('fifty_two_week_low_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('two_hundred_day_average_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('two_hundred_day_average_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('post_market_price', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('post_market_change', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('post_market_change_percent', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('post_market_time', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('pe_ttm', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('yield_ttm', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('trailing_three_month_returns', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('trailing_three_month_nav_returns', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('annual_return_nav_y3', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('annual_return_nav_y5', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('last_close_price_to_nnwc_per_share', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('last_close_tev_ebit_ltm', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('custom_price_alert_confidence', sa.String(length=16), nullable=True),
    sa.Column('prev_name', sa.String(length=255), nullable=True),
    sa.Column('ipo_expected_date', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('name_change_date', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('is_known', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('raw_json', mysql.LONGTEXT(), nullable=False),
    sa.PrimaryKeyConstraint('symbol', 'as_of_date'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('screen_runs',
    sa.Column('screen_key', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('fetched_rows', sa.Integer(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('page_count', mysql.SMALLINT(), nullable=False),
    sa.Column('yahoo_id', sa.String(length=64), nullable=True),
    sa.Column('version_id', sa.Integer(), nullable=True),
    sa.Column('last_updated', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('criteria_json', mysql.LONGTEXT(), nullable=True),
    sa.Column('content_hash', mysql.VARCHAR(charset='ascii', collation='ascii_general_ci', length=64), nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('screen_key', 'as_of_date'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_screen_runs_date', 'screen_runs', ['as_of_date'], unique=False)
    op.create_table('screens',
    sa.Column('screen_key', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('kind', sa.Enum('predefined', 'custom', name='screenkind'), nullable=False),
    sa.Column('quote_type', sa.Enum('EQUITY', 'MUTUALFUND', 'ETF', name='screenquotetype'), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('sort_field', sa.String(length=64), nullable=False),
    sa.Column('sort_asc', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('definition_json', mysql.LONGTEXT(), nullable=True),
    sa.Column('is_enabled', sa.Boolean(), server_default='1', nullable=False),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('screen_key'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('search_lists',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('list_key', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=128), nullable=False),
    sa.Column('rank_index', mysql.SMALLINT(), nullable=False),
    sa.Column('list_type', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('score', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('icon_url', sa.Text(), nullable=True),
    sa.Column('brand_slug', sa.String(length=64), nullable=True),
    sa.Column('pf_id', sa.String(length=128), nullable=True),
    sa.Column('user_id', sa.String(length=64), nullable=True),
    sa.Column('symbol_count', sa.Integer(), nullable=True),
    sa.Column('daily_percent_gain', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('follower_count', sa.Integer(), nullable=True),
    sa.Column('yahoo_id', sa.String(length=64), nullable=True),
    sa.Column('total', sa.Integer(), nullable=True),
    sa.Column('is_premium', sa.Boolean(), nullable=True),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('raw_json', mysql.LONGTEXT(), nullable=False),
    sa.PrimaryKeyConstraint('query_term', 'as_of_date', 'list_key'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('search_quotes',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('symbol', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=32), nullable=False),
    sa.Column('rank_index', mysql.SMALLINT(), nullable=False),
    sa.Column('score', sa.Numeric(precision=28, scale=12), nullable=True),
    sa.Column('quote_type', sa.String(length=32), nullable=True),
    sa.Column('type_disp', sa.String(length=64), nullable=True),
    sa.Column('exchange', sa.String(length=32), nullable=True),
    sa.Column('exch_disp', sa.String(length=64), nullable=True),
    sa.Column('short_name', sa.String(length=128), nullable=True),
    sa.Column('long_name', sa.String(length=255), nullable=True),
    sa.Column('sector', sa.String(length=64), nullable=True),
    sa.Column('sector_disp', sa.String(length=64), nullable=True),
    sa.Column('industry', sa.String(length=128), nullable=True),
    sa.Column('industry_disp', sa.String(length=128), nullable=True),
    sa.Column('disp_sec_ind_flag', sa.Boolean(), nullable=True),
    sa.Column('is_yahoo_finance', sa.Boolean(), nullable=True),
    sa.Column('prev_name', sa.String(length=255), nullable=True),
    sa.Column('name_change_date', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('is_known', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('raw_json', mysql.LONGTEXT(), nullable=False),
    sa.PrimaryKeyConstraint('query_term', 'as_of_date', 'symbol'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_search_quotes_symbol', 'search_quotes', ['symbol'], unique=False)
    op.create_table('search_report_hits',
    sa.Column('query_term', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('report_id', mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('rank_index', mysql.SMALLINT(), nullable=False),
    sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['research_reports.report_id'], onupdate='CASCADE', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('query_term', 'as_of_date', 'report_id'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.add_column('symbols', sa.Column('discovered_by', sa.String(length=16), server_default='manual', nullable=False))
    op.add_column('symbols', sa.Column('discovered_at', mysql.DATETIME(fsp=6), nullable=True))


def downgrade() -> None:
    op.drop_column('symbols', 'discovered_at')
    op.drop_column('symbols', 'discovered_by')
    op.drop_table('search_report_hits')
    op.drop_index('ix_search_quotes_symbol', table_name='search_quotes')
    op.drop_table('search_quotes')
    op.drop_table('search_lists')
    op.drop_table('screens')
    op.drop_index('ix_screen_runs_date', table_name='screen_runs')
    op.drop_table('screen_runs')
    op.drop_table('screen_quotes')
    op.drop_index('ix_screen_members_symbol', table_name='screen_members')
    op.drop_table('screen_members')
    op.drop_table('lookup_totals')
    op.drop_index('ix_lookup_results_symbol', table_name='lookup_results')
    op.drop_table('lookup_results')
    op.drop_index('ix_discovery_asof_dataset_date', table_name='discovery_asof_state')
    op.drop_table('discovery_asof_state')
