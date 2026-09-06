"""DB-based configuration table.

Holds DB overrides for `Settings` fields, in the same form `.env` gives
them: plain text. Duplicating the value type, range constraint, and
default in this table was deliberately rejected: if `Field(ge=1)` later
became `ge=2`, the copy here would go stale silently. `config.Settings`
is the single source of truth for that metadata.

Written engine-independent: it uses only `AsciiKeyType`, `Text`, and
`TsType`, and carries no engine-specific table arguments.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import AsciiKeyType, Base, TsType

SETTING_KEY_LENGTH = 64


class SettingRow(Base):
    """A single configuration override.

    Row count is not fixed: `set` adds a row, `unset` removes one, and a
    missing row is legitimate -- the only way to say "no override". NULL
    could not serve this role since an empty string is itself a valid
    value (`yf_news_tab=""`).
    """

    __tablename__ = "settings"

    # Named `setting_key`, not `key`: `KEY` is a reserved word in MySQL 8,
    # a trap the codebase has hit twice before (models/base.py
    # `bar_interval`, models/funds.py `holding_rank`). Collation "C" is
    # not there to prevent collisions -- it keeps a `YF_MAX_SHARDS` row
    # inserted via raw SQL visibly distinct from the canonical
    # `yf_max_shards`, so the loader's "unknown key" warning catches it.
    setting_key: Mapped[str] = mapped_column(AsciiKeyType(SETTING_KEY_LENGTH), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    # `func.now()`, not `func.now(6)`.
    #
    # This was a MySQL-legacy trap (`symbols` carried `now(6)` for a
    # while and broke migration): PostgreSQL has no `now(integer)`
    # function, so table creation fails ("function now(integer) does not
    # exist" -- measured). Argument-less `now()` already returns a
    # microsecond-precision `timestamptz` on PG, matching `TsType()`'s six
    # digits exactly.
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
