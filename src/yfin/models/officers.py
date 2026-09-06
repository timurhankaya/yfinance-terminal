"""company_officers (S5.2). info'nun ham ciktisindan turer, ayri ag cagrisi yok."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    Base,
    BigNumType,
    PersonNameType,
    symbol_fk_column,
)


class CompanyOfficer(Base):
    """Surrogate id YOKTUR: dogal anahtar yeterlidir ve her upsert'te
    yapay anahtar tuketilmesini onler (S5.2)."""

    __tablename__ = "company_officers"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    # 'Tim Cook' != 'TIM COOK' olmali -> COLLATE "C" (S5.1)
    name: Mapped[str] = mapped_column(PersonNameType(), primary_key=True)

    title: Mapped[str | None] = mapped_column(String(255, collation="C"))
    age: Mapped[int | None] = mapped_column(Integer)
    year_born: Mapped[int | None] = mapped_column(Integer)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    total_pay: Mapped[Decimal | None] = mapped_column(BigNumType())
    exercised_value: Mapped[Decimal | None] = mapped_column(BigNumType())
    unexercised_value: Mapped[Decimal | None] = mapped_column(BigNumType())
