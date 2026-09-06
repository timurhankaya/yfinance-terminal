"""advisory_lock behavior and the content of its error message.

The message content is not cosmetic: a bare "could not acquire advisory lock"
sent a 2026-09-04 debugging session down the wrong path when two live runs
were accidentally started concurrently. The symptom (18 ERRORs and
inconsistent row counts in module fixtures) looked like a code bug. A message
naming who holds the lock would have resolved it in one step.

`pg_locks JOIN pg_stat_activity` reports pid, application_name and the
running query, which is why this message is richer than a raw connection id.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from yfin.db import SYNC_LOCK_NAME, LockNotAcquired, advisory_lock, lock_holder

pytestmark = pytest.mark.repo

TEST_LOCK = "yfin_test_lock"


def test_lock_is_acquired_and_released(test_engine: Engine) -> None:
    with advisory_lock(test_engine, name=TEST_LOCK), test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is not None, "lock was not acquired"

    with test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is None


def test_second_holder_gets_a_message_naming_the_first(test_engine: Engine) -> None:
    """The message must describe who holds the lock."""
    with (
        advisory_lock(test_engine, name=TEST_LOCK),
        pytest.raises(LockNotAcquired) as excinfo,
        advisory_lock(test_engine, name=TEST_LOCK),
    ):
        pass

    message = str(excinfo.value)
    assert TEST_LOCK in message
    assert "pid=" in message, "message does not say who holds the lock"
    # pid must be numeric
    assert any(part.isdigit() for part in message.replace("pid=", " ").split())
    # application_name is the second half of the diagnosis: which shard/process
    assert "application_name=" in message


def test_lock_is_released_even_when_the_body_raises(test_engine: Engine) -> None:
    """pg_advisory_unlock must run even if the body raises, or the lock leaks
    for the rest of the session and the next run fails for no visible reason."""
    with pytest.raises(RuntimeError), advisory_lock(test_engine, name=TEST_LOCK):
        raise RuntimeError("boom")

    with test_engine.connect() as conn:
        assert lock_holder(conn, TEST_LOCK) is None


def test_sync_lock_name_is_the_documented_one() -> None:
    """The conftest guard and error messages are written against this name."""
    assert SYNC_LOCK_NAME == "yfin_sync"
