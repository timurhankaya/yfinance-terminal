"""Field listesinden SQLAlchemy kolonu ureten fabrika (S5.4)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column

from yfin.models.fields import Field
from yfin.models.kinds import KINDS

MYSQL_ROW_SIZE_LIMIT = 65535


def make_column(field: Field) -> Column[Any]:
    """Tipli kolon; hepsi NULL kabul eder (kaynak alan seti sembole gore
    degisir)."""
    return Column(field.column, KINDS[field.kind].sql_type(), nullable=True)


def estimated_row_size(fields: tuple[Field, ...], extra: int = 0) -> int:
    """S5.4 satir butcesi kontrolu. Kolon terfisi bu deger gorulmeden yapilmaz."""
    return sum(KINDS[f.kind].row_cost for f in fields) + extra
