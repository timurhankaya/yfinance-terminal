"""As-of gate table.

Carries no data of its own: it records, per (symbol, dataset), when the
content last changed and when it was last verified. The hash lives here
rather than in the data tables because `funds_data` writes to four
tables -- copying the hash into each row would create four sources of
truth for the same question.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    HashType,
    TsType,
    symbol_fk_column,
)


class AsOfState(Base):
    __tablename__ = "asof_state"
    __table_args__ = (
        Index("ix_asof_state_dataset_date", "dataset", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # As-of date of the last change.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    # For audit only; the gate decision looks at the hash alone, not this.
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Written on first INSERT and never updated again -- kept out of
    # update_columns, or ON DUPLICATE KEY UPDATE would overwrite it.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Last verification time: updated even when the hash is unchanged
    # (hash_gated.py's policy), so "when was this symbol last checked"
    # always has an answer.
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
