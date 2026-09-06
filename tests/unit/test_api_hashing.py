"""Secret hashing, and the constant-shape verification path."""

from __future__ import annotations

import pytest

from yfin.api.auth import hashing
from yfin.api.auth.hashing import (
    MEMORY_COST_KIB,
    PARALLELISM,
    TIME_COST,
    VERIFICATIONS_PER_ATTEMPT,
    Candidate,
    hash_secret,
    new_client_id,
    new_secret,
    verify_against,
    verify_secret,
)
from yfin.api.models.clients import CLIENT_ID_LENGTH, CLIENT_ID_PREFIX, ApiScope
from yfin.api.models.plans import UsageFamily
from yfin.core.families import EXTRA_USAGE_FAMILIES, DataFamily, scope_for


def test_argon2_parameters_are_PINNED() -> None:
    """Pinned deliberately. The decoy hash used to pad the verification
    path is built with these; if they drift, the decoy stops costing what
    a real comparison costs and the timing signal comes back."""
    assert (TIME_COST, MEMORY_COST_KIB, PARALLELISM) == (2, 19456, 1)


def test_hash_verifies_and_a_wrong_credential_is_rejected() -> None:
    secret = new_secret()
    digest = hash_secret(secret)
    assert verify_secret(secret, digest)
    assert not verify_secret(new_secret(), digest)


def test_a_malformed_hash_returns_False_instead_of_raising() -> None:
    assert not verify_secret("x", "this is not an argon2 hash")


def test_client_id_format() -> None:
    client_id = new_client_id()
    assert client_id.startswith(CLIENT_ID_PREFIX)
    assert len(client_id) == CLIENT_ID_LENGTH


def _count_verifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = hashing.verify_secret

    def counting(secret: str, secret_hash: str) -> bool:
        calls.append(secret_hash)
        return original(secret, secret_hash)

    monkeypatch.setattr(hashing, "verify_secret", counting)
    return calls


@pytest.mark.parametrize("stored", [0, 1, 2])
def test_verification_ALWAYS_runs_twice(
    stored: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise response time answers what the response body refuses to:
    does this client exist, and does it hold one secret or two."""
    calls = _count_verifications(monkeypatch)
    candidates = [
        Candidate(secret_id=i, secret_hash=hash_secret(new_secret()), is_usable=True)
        for i in range(stored)
    ]
    verify_against(new_secret(), candidates)
    assert len(calls) == VERIFICATIONS_PER_ATTEMPT


def test_the_correct_credential_matches() -> None:
    secret = new_secret()
    candidate = Candidate(secret_id=7, secret_hash=hash_secret(secret), is_usable=True)
    assert verify_against(secret, [candidate]) == 7


def test_both_of_two_credentials_match() -> None:
    """The rotation window: both the new and the outgoing secret work."""
    old, new = new_secret(), new_secret()
    candidates = [
        Candidate(secret_id=1, secret_hash=hash_secret(new), is_usable=True),
        Candidate(secret_id=2, secret_hash=hash_secret(old), is_usable=True),
    ]
    assert verify_against(old, candidates) == 2
    assert verify_against(new, candidates) == 1


def test_a_revoked_credential_DOES_NOT_match_but_is_STILL_hashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the hash for a revoked secret would make it
    distinguishable from a wrong one by timing alone."""
    calls = _count_verifications(monkeypatch)
    secret = new_secret()
    candidate = Candidate(secret_id=3, secret_hash=hash_secret(secret), is_usable=False)
    assert verify_against(secret, [candidate]) is None
    assert len(calls) == VERIFICATIONS_PER_ATTEMPT


# --- family / scope derivation ----------------------------------------------


def test_scope_set_DERIVES_from_the_family_set() -> None:
    """A scope with no family would be unreachable; a family with no scope
    would be silently open. Neither may happen, so the two are derived
    from one list rather than typed twice."""
    assert {s.value for s in ApiScope} == {scope_for(f) for f in DataFamily}


def test_usage_families_are_the_data_families_plus_two_surfaces() -> None:
    assert {u.value for u in UsageFamily} == {f.value for f in DataFamily} | set(
        EXTRA_USAGE_FAMILIES
    )
