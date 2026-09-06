"""price_bars aylik bakimi (PB S7.5). SILME YAPMAZ.

Kalici arsivde bakim, budama demek DEGILDIR: veri geri getirilemez.
Buradaki isler partition ilerletme, oksuz satir denetimi ve raporlamadir.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# Bakimin ne kadar ileriyi kapsamasi gerektigi. 12 ay, aylik bir isin bir
# kez kacirilmasini bile tolere eder.
LOOKAHEAD_MONTHS = 12


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _partition_name(year: int, month: int) -> str:
    return f"p{year:04d}_{month:02d}"


def existing_partitions(session: Session) -> set[str]:
    rows = session.execute(
        text(
            "SELECT partition_name FROM information_schema.partitions "
            "WHERE table_schema = DATABASE() AND table_name = 'price_bars' "
            "AND partition_name IS NOT NULL"
        )
    ).scalars()
    return set(rows)


def missing_partitions(session: Session, *, today: date | None = None) -> list[str]:
    """Onumuzdeki LOOKAHEAD_MONTHS ay icin eksik partition adlari.

    Bunlar eklenmezse aralik disi insert ERROR 1526 ile duser (MAXVALUE
    bolumu bilincli olarak YOKTUR, PB S5.3) - yani eksiklik sessiz bir
    performans coku degil, gorunur bir kosu hatasi olur.
    """
    current = existing_partitions(session)
    day = today or date.today()
    year, month = day.year, day.month
    missing: list[str] = []
    for _ in range(LOOKAHEAD_MONTHS):
        name = _partition_name(year, month)
        if name not in current:
            missing.append(name)
        year, month = _month_after(year, month)
    return missing


def add_partitions(session: Session, names: list[str]) -> None:
    """Eksik partition'lari ekler.

    MAXVALUE bolumu olmadigi icin ADD PARTITION metadata-only ve anliktir.
    (MAXVALUE olsaydi bu islem IMKANSIZ olurdu - ERROR 1493 - ve tek yol
    her satiri kopyalayan REORGANIZE PARTITION olurdu.)

    Partition'lar ARTAN sirada eklenmek zorundadir; siralama burada
    yapilir, cagirana birakilmaz.
    """
    for name in sorted(names):
        year, month = int(name[1:5]), int(name[6:8])
        next_year, next_month = _month_after(year, month)
        session.execute(
            text(
                f"ALTER TABLE price_bars ADD PARTITION "
                f"(PARTITION {name} VALUES LESS THAN "
                f"('{next_year:04d}-{next_month:02d}-01'))"
            )
        )
