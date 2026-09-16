"""DB-based configuration table.

Holds DB overrides for `Settings` fields as plain text, the same form
`.env` gives them; type, range and default live only in `config.Settings`
so a copy here cannot go stale."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import AsciiKeyType, Base, TsType

SETTING_KEY_LENGTH = 64


class SettingRow(Base):
    """A single configuration override.

    A missing row is the only way to say "no override": NULL cannot serve,
    since an empty string is itself a valid value."""

    __tablename__ = "settings"

    # Collation "C" keeps a wrongly cased key inserted via raw SQL visibly
    # distinct from the canonical one, so the loader's "unknown key"
    # warning catches it.
    setting_key: Mapped[str] = mapped_column(AsciiKeyType(SETTING_KEY_LENGTH), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    # `func.now()`, not `func.now(6)`: PostgreSQL has no `now(integer)`, and
    # bare `now()` already returns microsecond precision.
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
