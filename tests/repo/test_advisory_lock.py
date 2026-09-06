"""advisory_lock davranisi ve HATA MESAJININ ICERIGI.

Mesajin icerigi bir "kozmetik" ayrinti degildir: ciplak "advisory lock
alinamadi" mesaji 2026-09-04'te bir oturumu yanlis yere baktirdi. Iki
live kosusu yanlislikla ust uste baslatilmisti; belirti modul
fixture'larinda 18 ERROR ve tutarsiz satir sayilariydi, yani KOD HATASI
gibi gorunuyordu. Kilidi KIMIN tuttugunu soyleyen bir mesaj bunu tek
adimda cozerdi.

PostgreSQL'de mesaj MySQL'dekinden DAHA zengindir: `IS_USED_LOCK`
yalnizca bir connection id donduruyordu, `pg_locks JOIN
pg_stat_activity` ise pid, application_name ve calisan sorguyu verir
(PG S5.3).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from yfin.db import SYNC_LOCK_NAME, LockNotAcquired, advisory_lock, lock_holder

pytestmark = pytest.mark.repo

TEST_LOCK = "yfin_test_lock"


def test_lock_is_acquired_and_released(test_engine: Engine) -> None:
    with advisory_lock(test_engine, name=TEST_LOCK), test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is not None, "kilit alinmamis"

    with test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is None


def test_second_holder_gets_a_message_naming_the_first(test_engine: Engine) -> None:
    """Mesaj kilidi tutan oturumu TARIF ETMELI."""
    with (
        advisory_lock(test_engine, name=TEST_LOCK),
        pytest.raises(LockNotAcquired) as excinfo,
        advisory_lock(test_engine, name=TEST_LOCK),
    ):
        pass

    message = str(excinfo.value)
    assert TEST_LOCK in message
    assert "pid=" in message, "kilidi kimin tuttugu soylenmiyor"
    # pid sayisal olmali
    assert any(part.isdigit() for part in message.replace("pid=", " ").split())
    # application_name teshisin ikinci yarisidir: hangi shard/surec
    assert "application_name=" in message


def test_lock_is_released_even_when_the_body_raises(test_engine: Engine) -> None:
    """Govde patlasa da pg_advisory_unlock cagrilmali; aksi halde kilit
    OTURUM sonuna kadar sizar ve sonraki kosu sebepsiz duser."""
    with pytest.raises(RuntimeError), advisory_lock(test_engine, name=TEST_LOCK):
        raise RuntimeError("patlama")

    with test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is None


def test_sync_lock_name_is_the_documented_one() -> None:
    """conftest guard'i ve hata mesajlari bu ada gore yazildi."""
    assert SYNC_LOCK_NAME == "yfin_sync"
