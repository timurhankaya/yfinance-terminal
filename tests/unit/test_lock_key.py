"""Advisory kilit anahtari (PG S5.1). Veritabanina DOKUNMAZ."""

from __future__ import annotations

from yfin.db import SYNC_LOCK_NAME, _lock_key, _lock_key_parts


def test_key_is_deterministic() -> None:
    assert _lock_key("yfin_sync") == _lock_key("yfin_sync")
    assert _lock_key("a") != _lock_key("b")


def test_key_fits_signed_bigint() -> None:
    """pg_try_advisory_lock imzali bigint alir."""
    key = _lock_key(SYNC_LOCK_NAME)
    assert -(2**63) <= key < 2**63


def test_parts_are_valid_oids() -> None:
    """classid/objid `oid`dir: ISARETSIZ 32-bit.

    Gercek anahtar NEGATIFTIR ve classid 2^31'in USTUNDEDIR. Ayristirma
    SQL'de `(:key >> 32)::int` ile yapilsaydi iki ayri hata cikardi:
    negatif anahtarda `>>` isareti uzatir ve yanlis classid uretir; alt
    32 bit 2^31'i astiginda `::int` ERROR 22003 verir. Bu testin
    `key < 0` ve `classid >= 2**31` iddialari, hatanin TEORIK OLMADIGINI
    kayit altina alir.
    """
    key = _lock_key(SYNC_LOCK_NAME)
    assert key < 0, "bu testin anlamli olmasi icin anahtar negatif olmali"
    classid, objid = _lock_key_parts(key)
    assert 0 <= classid < 2**32
    assert 0 <= objid < 2**32
    assert classid >= 2**31, "gercek anahtarda classid 2^31 ustunde"


def test_parts_roundtrip() -> None:
    for name in ("yfin_sync", "yfin_test_lock", ""):
        key = _lock_key(name)
        classid, objid = _lock_key_parts(key)
        assert (classid << 32 | objid) == (key & 0xFFFF_FFFF_FFFF_FFFF)
