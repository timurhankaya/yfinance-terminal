"""statement_kind ENUM'una 'valuation' degeri

Revision ID: 3f5c81ae90d4
Revises: 847b03d6b8cd
Create Date: 2026-09-04 15:20:00.000000+00:00

`get_valuation_measures` kendi tablosunu ACMAZ: cercevesi finansal
tablolarla ayni sekildedir ve mevcut EAV semasina (`financial_periods` /
`financial_facts`) dorduncu bir `statement` degeri olarak girer. Sema
degisikligi bu tek ENUM genislemesinden ibarettir.

ENUM iki tabloda PAYLASILIR ve `fk_financial_facts_period` bilesik FK'si
bu kolonu tasir; MySQL FK'li bir kolonun tipini degistirmeyi reddeder, bu
yuzden iki ALTER `FOREIGN_KEY_CHECKS=0` arasinda calisir. Deger listeye
SONA eklenir: ENUM sirasi ORDINAL'dir, araya girmek mevcut satirlarin
anlamini kaydirirdi.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3f5c81ae90d4"
down_revision: str | None = "847b03d6b8cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "ENUM('income','balance_sheet','cash_flow')"
_NEW = "ENUM('income','balance_sheet','cash_flow','valuation')"
_TABLES = ("financial_periods", "financial_facts")


def _alter(definition: str) -> None:
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    try:
        for table in _TABLES:
            op.execute(f"ALTER TABLE {table} MODIFY COLUMN statement {definition} NOT NULL")
    finally:
        op.execute("SET FOREIGN_KEY_CHECKS = 1")


def upgrade() -> None:
    _alter(_NEW)


def downgrade() -> None:
    # Once satirlar duser (FK checks ACIK: cocuk satirlari ON DELETE CASCADE
    # ile gider), sonra ENUM daralir. Ters sirada yapilsaydi MySQL 'valuation'
    # satirlarini bos dizeye cevirip veriyi sessizce bozardi.
    op.execute("DELETE FROM financial_periods WHERE statement = 'valuation'")
    _alter(_OLD)
