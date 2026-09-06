"""Field listesinden SQLAlchemy kolonu ureten fabrika (PG S2.6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column

from yfin.models.fields import Field
from yfin.models.kinds import KINDS

# SATIR BUTCESI KAVRAMI YOKTUR ve yerine BIR SEY KONMADI.
#
# MySQL'in 65 535 baytlik satir siniri InnoDB'nin sayfa ici satir
# formatindan geliyordu ve `utf8mb4` VARCHAR(255) basina ~1 022 bayt
# sayiyordu; bu yuzden `MYSQL_ROW_SIZE_LIMIT` ve `estimated_row_size()`
# ile bir butce tutuluyor, `kinds.py` her kind icin bir `row_cost`
# tasiyordu.
#
# PostgreSQL'de boyle bir sinir yoktur: satir 8 KB sayfaya sigmazsa genis
# degerler otomatik olarak TOAST tablosuna tasinir. Pratik sinir kolon
# SAYISIDIR (1 600) ve en genis tablomuz olan ticker_info bunun cok
# altindadir.
#
# Bu, migrasyonun bilincli olarak kaybettigi TEK koruma katmanidir.
# Yerine ikame bir savunma icat edilmedi cunku korunacak bir sinir
# kalmadi; uydurma bir butce yaniltici olurdu.


def make_column(field: Field) -> Column[Any]:
    """Tipli kolon; hepsi NULL kabul eder (kaynak alan seti sembole gore
    degisir).

    `ubig` kind'inda CHECK eklenir: PostgreSQL'de unsigned tamsayi
    YOKTUR, `BIGINT UNSIGNED`in verdigi "negatif olamaz" garantisi kisitla
    yeniden kurulur.

    Kisit ADSIZ birakilir; adini `Base.metadata`nin naming_convention'i
    uretir (`ck_{table}_{column}`). Ad burada URETILEMEZDI: `Field`
    (models/fields.py) tablo adini TASIMAZ ve tek cagri yeri
    snapshots.py'dir. Adlandirmayi SQLAlchemy'ye devretmek ayrica
    Alembic'in kararsiz ad problemini kaynaginda cozer (PG S2.6).
    """
    spec = KINDS[field.kind]
    args: list[Any] = [field.column, spec.sql_type()]
    if field.kind == "ubig":
        args.append(CheckConstraint(f'"{field.column}" >= 0'))
    return Column(*args, nullable=True)
