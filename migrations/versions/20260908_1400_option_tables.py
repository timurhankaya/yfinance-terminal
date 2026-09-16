"""option expirations and chains

Revision ID: 9d41c6f2b7ae
Revises: 5e7eda819cc6
Create Date: 2026-09-08 14:00:00.000000+00:00"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '9d41c6f2b7ae'
down_revision: str | None = '5e7eda819cc6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `create_type=False` is the whole point of reaching for the dialect
    # type here: the enum is created once, on the line below, and the
    # `create_table` further down must not try again. A plain `sa.Enum`
    # column emits its own CREATE TYPE, and the second one fails the
    # migration with `type "option_type" already exists`.
    option_type = postgresql.ENUM('call', 'put', name='option_type', create_type=False)
    option_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'option_expirations',
        sa.Column('symbol', sa.VARCHAR(length=32, collation='C'), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('expiry_date', sa.Date(), nullable=False),
        sa.Column('fetched_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['symbol'], ['symbols.symbol'], onupdate='CASCADE', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('symbol', 'as_of_date', 'expiry_date'),
    )
    op.create_index('ix_option_expirations_as_of', 'option_expirations', ['as_of_date'])

    op.create_table(
        'option_quotes',
        sa.Column('symbol', sa.VARCHAR(length=32, collation='C'), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('expiry_date', sa.Date(), nullable=False),
        sa.Column('option_type', option_type, nullable=False),
        sa.Column('contract_symbol', sa.VARCHAR(length=32, collation='C'), nullable=False),
        sa.Column('strike', sa.Numeric(precision=28, scale=12), nullable=False),
        sa.Column('last_price', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('bid', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('ask', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('change', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('percent_change', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('implied_volatility', sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column('volume', sa.BigInteger(), nullable=True),
        sa.Column('open_interest', sa.BigInteger(), nullable=True),
        sa.Column('in_the_money', sa.Boolean(), nullable=True),
        sa.Column('contract_size', sa.VARCHAR(length=16, collation='C'), nullable=True),
        sa.Column('currency', sa.VARCHAR(length=8, collation='C'), nullable=True),
        sa.Column('last_trade_ts_utc', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('fetched_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint('"strike" >= 0', name='ck_option_quotes_strike_nonneg'),
        sa.ForeignKeyConstraint(['symbol'], ['symbols.symbol'], onupdate='CASCADE', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('symbol', 'as_of_date', 'expiry_date', 'option_type', 'contract_symbol'),
    )
    op.create_index('ix_option_quotes_as_of', 'option_quotes', ['as_of_date'])
    op.create_index('ix_option_quotes_expiry', 'option_quotes', ['expiry_date'])


def downgrade() -> None:
    op.drop_index('ix_option_quotes_expiry', table_name='option_quotes')
    op.drop_index('ix_option_quotes_as_of', table_name='option_quotes')
    op.drop_table('option_quotes')
    op.drop_index('ix_option_expirations_as_of', table_name='option_expirations')
    op.drop_table('option_expirations')
    sa.Enum(name='option_type').drop(op.get_bind(), checkfirst=True)
