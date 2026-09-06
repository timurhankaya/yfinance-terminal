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


def make_column(field: Field, table_name: str) -> Column[Any]:
    """Tipli kolon; hepsi NULL kabul eder (kaynak alan seti sembole gore
    degisir).

    `ubig` kind'inda CHECK eklenir: PostgreSQL'de unsigned tamsayi
    YOKTUR, `BIGINT UNSIGNED`in verdigi "negatif olamaz" garantisi kisitla
    yeniden kurulur.

    KISIT ACIKCA ADLANDIRILIR ve `table_name` TAM DA BUNUN ICIN alinir.
    Ilk tasarim kisiti adsiz birakip adi `Base.metadata`nin
    naming_convention'ina (`ck_%(table_name)s_%(column_0_name)s`)
    biraktiriyordu. OLCULDU: KOLON SEVIYESINDE tanimlanan bir
    CheckConstraint icin SQLAlchemy `column_0_name`i cozemez ve ad
    `ck_ticker_info_` olarak -- kolon kismi BOS -- uretilir. ticker_info'da
    sekiz `ubig` kolonu vardir, yani sekiz kisit AYNI ADI alir ve
    PostgreSQL `CREATE TABLE`i reddeder.

    `Field` (models/fields.py) tablo adini tasimadigi icin ad buradan
    baska bir yerde de uretilemezdi; iki cagri yeri de (snapshots.py,
    discovery.py) tablo adini zaten biliyor.
    """
    spec = KINDS[field.kind]
    args: list[Any] = [field.column, spec.sql_type()]
    if spec.check is not None:
        args.append(
            CheckConstraint(
                spec.check(field.column),
                name=f"ck_{table_name}_{field.column}_nonneg",
            )
        )
    return Column(*args, nullable=True)
