"""Domain semasi: collation, CHECK kisiti ve FK'ler (SI S9.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.repo

NOW = "2026-09-04 12:00:00"


def _seed_symbol(session: Session, symbol: str) -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, 0, 0, :t, :t)"
        ),
        {"s": symbol, "t": NOW},
    )


def _insert_domain(
    session: Session,
    key: str,
    symbol: str,
    *,
    domain_type: str = "sector",
    parent: str | None = None,
) -> None:
    session.execute(
        text(
            "INSERT INTO domains "
            "(domain_key, domain_type, symbol, parent_key, name, first_seen_at, fetched_at) "
            "VALUES (:k, :dt, :s, :p, :n, :t, :t)"
        ),
        {"k": key, "dt": domain_type, "s": symbol, "p": parent, "n": key, "t": NOW},
    )


def test_domain_key_is_case_sensitive(db_session: Session) -> None:
    """`TECHNOLOGY` canlida 404 verdi; anahtarlar buyuk/kucuk harf DUYARLI.

    `ascii_general_ci` olsaydi iki ayri anahtar tek satira inerdi.
    """
    _seed_symbol(db_session, "^YHZ1")
    _seed_symbol(db_session, "^YHZ2")
    _insert_domain(db_session, "technology", "^YHZ1")
    _insert_domain(db_session, "TECHNOLOGY", "^YHZ2")
    count = db_session.execute(
        text("SELECT COUNT(*) FROM domains WHERE LOWER(domain_key) = 'technology'")
    ).scalar_one()
    assert count == 2


def test_check_constraint_rejects_a_parentless_industry(db_session: Session) -> None:
    _seed_symbol(db_session, "^YHZ3")
    with pytest.raises((IntegrityError, OperationalError)):
        _insert_domain(db_session, "semis", "^YHZ3", domain_type="industry")
        db_session.flush()


def test_check_constraint_allows_a_parentless_sector(db_session: Session) -> None:
    _seed_symbol(db_session, "^YHZ4")
    _insert_domain(db_session, "energy", "^YHZ4")
    db_session.flush()


def test_symbol_is_unique_across_domains(db_session: Session) -> None:
    _seed_symbol(db_session, "^YHZ5")
    _insert_domain(db_session, "energy", "^YHZ5")
    with pytest.raises(IntegrityError):
        _insert_domain(db_session, "utilities", "^YHZ5")
        db_session.flush()


def test_deleting_a_sector_with_children_is_restricted(db_session: Session) -> None:
    """ON DELETE RESTRICT SELF-FK'de de gecerli: 145 endustri oksuz kalamaz."""
    _seed_symbol(db_session, "^YHZ6")
    _seed_symbol(db_session, "^YHZ7")
    _insert_domain(db_session, "energy", "^YHZ6")
    _insert_domain(db_session, "oil-gas", "^YHZ7", domain_type="industry", parent="energy")
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM domains WHERE domain_key = 'energy'"))
        db_session.flush()


def test_deleting_a_domain_symbol_is_restricted(db_session: Session) -> None:
    _seed_symbol(db_session, "^YHZ8")
    _insert_domain(db_session, "energy", "^YHZ8")
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM symbols WHERE symbol = '^YHZ8'"))
        db_session.flush()


def test_report_links_cascade_when_the_report_is_deleted(db_session: Session) -> None:
    """FK TERS YONDE calisir: rapor silinince bag silinir.

    Bu yuzden `--orphan-reports` ayri bir adimdir (SI S11.2).
    """
    _seed_symbol(db_session, "^YHZ9")
    _insert_domain(db_session, "energy", "^YHZ9")
    db_session.execute(
        text(
            "INSERT INTO research_reports "
            "(report_id, as_of_date, first_seen_at, fetched_at) VALUES ('R1', '2026-09-04', :t, :t)"
        ),
        {"t": NOW},
    )
    db_session.execute(
        text(
            "INSERT INTO domain_report_links "
            "(domain_key, as_of_date, report_id, position, fetched_at) "
            "VALUES ('energy', '2026-09-04', 'R1', 0, :t)"
        ),
        {"t": NOW},
    )
    db_session.flush()
    db_session.execute(text("DELETE FROM research_reports WHERE report_id = 'R1'"))
    remaining = db_session.execute(text("SELECT COUNT(*) FROM domain_report_links")).scalar_one()
    assert remaining == 0


def test_report_title_is_mediumtext(db_session: Session) -> None:
    """`TEXT` 65 535 BAYT'tir; olculen max 23 570 KARAKTER utf8mb4'te tasabilir."""
    row = db_session.execute(
        text(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'research_reports' "
            "AND COLUMN_NAME = 'report_title'"
        )
    ).scalar_one()
    assert row == "mediumtext"


def test_domain_asof_state_key_includes_region(db_session: Session) -> None:
    keys = list(
        db_session.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'domain_asof_state' "
                "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
            )
        ).scalars()
    )
    assert keys == ["domain_key", "dataset", "region"]
