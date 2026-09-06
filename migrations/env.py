"""Alembic ortami: baglanti bilgisi .env'den gelir, alembic.ini'ye yazilmaz."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from yfin.config import get_settings
from yfin.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic ifade tabanli indexleri (fetched_at DESC) DB'den geri okuyup
# karsilastiramaz; her calistirmada sahte bir "degisti" uretir. Bu iki
# index elle yonetilir ve autogenerate karsilastirmasindan haric tutulur.
EXPRESSION_INDEXES = frozenset(
    {
        "ix_ticker_info_history_symbol_fetched",
        "ix_ticker_fast_info_history_symbol_fetched",
        # models/financials.py:237 -> Index(..., desc(text("filing_date")))
        "ix_sec_filings_symbol_date",
    }
)


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
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
