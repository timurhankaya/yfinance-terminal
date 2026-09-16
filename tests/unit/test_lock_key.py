"""Advisory lock key. Does not touch a database."""

from __future__ import annotations

from yfin.storage.db import SYNC_LOCK_NAME, _lock_key, _lock_key_parts


def test_key_is_deterministic() -> None:
    assert _lock_key("yfin_sync") == _lock_key("yfin_sync")
    assert _lock_key("a") != _lock_key("b")


def test_key_fits_signed_bigint() -> None:
    """pg_try_advisory_lock takes a signed bigint."""
    key = _lock_key(SYNC_LOCK_NAME)
    assert -(2**63) <= key < 2**63


def test_parts_are_valid_oids() -> None:
    """classid/objid are `oid` (unsigned 32-bit) and the key is negative with classid above
    2^31, so a SQL split via `(:key >> 32)::int` would sign-extend and overflow `int`."""
    key = _lock_key(SYNC_LOCK_NAME)
    assert key < 0, "the key must be negative for this test to be meaningful"
    classid, objid = _lock_key_parts(key)
    assert 0 <= classid < 2**32
    assert 0 <= objid < 2**32
    assert classid >= 2**31, "classid is above 2^31 for the real key"


def test_parts_roundtrip() -> None:
    for name in ("yfin_sync", "yfin_test_lock", ""):
        key = _lock_key(name)
        classid, objid = _lock_key_parts(key)
        assert (classid << 32 | objid) == (key & 0xFFFF_FFFF_FFFF_FFFF)
