"""advisory_lock davranisi ve HATA MESAJININ ICERIGI.

Mesajin icerigi bir "kozmetik" ayrinti degildir: ciplak "advisory lock
alinamadi" mesaji 2026-09-04'te bir oturumu yanlis yere baktirdi. Iki
live kosusu yanlislikla ust uste baslatilmisti; belirti modul
fixture'larinda 18 ERROR ve tutarsiz satir sayilariydi, yani KOD HATASI
gibi gorunuyordu. Kilidi KIMIN tuttugunu soyleyen bir mesaj bunu tek
adimda cozerdi.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from yfin.db import SYNC_LOCK_NAME, LockNotAcquired, advisory_lock

pytestmark = pytest.mark.repo


def test_lock_is_acquired_and_released(test_engine: Engine) -> None:
    with advisory_lock(test_engine, name="yfin_test_lock"):
        with test_engine.connect() as conn:
            free = conn.execute(text("SELECT IS_FREE_LOCK('yfin_test_lock')")).scalar()
        assert free == 0, "kilit alinmamis"

    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT IS_FREE_LOCK('yfin_test_lock')")).scalar() == 1


def test_second_holder_gets_a_message_naming_the_first(test_engine: Engine) -> None:
    """Mesaj kilidi tutan connection'i ADIYLA soylemeli."""
    with (
        advisory_lock(test_engine, name="yfin_test_lock"),
        pytest.raises(LockNotAcquired) as excinfo,
        advisory_lock(test_engine, name="yfin_test_lock"),
    ):
        pass

    message = str(excinfo.value)
    assert "yfin_test_lock" in message
    assert "connection" in message, "kilidi kimin tuttugu soylenmiyor"
    # Connection id sayisal olmali (IS_USED_LOCK sonucu)
    assert any(part.isdigit() for part in message.replace("(", " ").split())
    assert "PROCESSLIST" in message, "kullaniciya sonraki adim soylenmiyor"


def test_lock_is_released_even_when_the_body_raises(test_engine: Engine) -> None:
    """Govde patlasa da RELEASE_LOCK cagrilmali; aksi halde kilit
    surecin sonuna kadar sizar ve sonraki kosu sebepsiz duser."""
    with pytest.raises(RuntimeError), advisory_lock(test_engine, name="yfin_test_lock"):
        raise RuntimeError("patlama")

    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT IS_FREE_LOCK('yfin_test_lock')")).scalar() == 1


def test_sync_lock_name_is_the_documented_one() -> None:
    """conftest guard'i ve hata mesajlari bu ada gore yazildi."""
    assert SYNC_LOCK_NAME == "yfin_sync"
