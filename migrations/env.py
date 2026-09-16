"""Alembic environment: connection info comes from .env, not alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from yfin.core.config import get_settings
from yfin.models import Base

# Imported for the side effect: the API tables share this Base, and
# without the import autogenerate would see them as absent and write a
# migration that drops them.
import yfin.api.models  # noqa: F401,E402  isort:skip

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic cannot read expression-based indexes (fetched_at DESC) back from
# the DB to compare them, so it reports a false "changed" every run. These
# indexes are managed by hand and excluded from autogenerate comparison.
EXPRESSION_INDEXES = frozenset(
    {
        "ix_ticker_info_history_symbol_fetched",
        "ix_ticker_fast_info_history_symbol_fetched",
        # models/financials.py:237 -> Index(..., desc(text("filing_date")))
        "ix_sec_filings_symbol_date",
    }
)


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    """Exclude only the expression-based indexes.

    No `_timescaledb` schema filter: `include_schemas` is False, so chunk tables
    never appear, and `create_hypertable`'s default index lives in `public`
    anyway, which is why `create_default_indexes=False` is used instead."""
    return not (type_ == "index" and name in EXPRESSION_INDEXES)


config.set_main_option("sqlalchemy.url", get_settings().db_url().render_as_string(False))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
