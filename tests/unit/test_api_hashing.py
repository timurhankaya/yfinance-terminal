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


def test_argon2_parametreleri_SABIT() -> None:
    """Pinned deliberately. The decoy hash used to pad the verification
    path is built with these; if they drift, the decoy stops costing what
    a real comparison costs and the timing signal comes back."""
    assert (TIME_COST, MEMORY_COST_KIB, PARALLELISM) == (2, 19456, 1)


def test_hash_dogrulanir_ve_yanlis_secret_reddedilir() -> None:
    secret = new_secret()
    digest = hash_secret(secret)
    assert verify_secret(secret, digest)
    assert not verify_secret(new_secret(), digest)


def test_bozuk_hash_istisna_degil_False_dondurur() -> None:
    assert not verify_secret("x", "bu bir argon2 hash'i degil")


def test_client_id_bicimi() -> None:
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
def test_dogrulama_HER_ZAMAN_iki_kez_kosar(
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


def test_dogru_secret_eslesir() -> None:
    secret = new_secret()
    candidate = Candidate(secret_id=7, secret_hash=hash_secret(secret), is_usable=True)
    assert verify_against(secret, [candidate]) == 7


def test_iki_secretten_ikincisi_de_eslesir() -> None:
    """The rotation window: both the new and the outgoing secret work."""
    old, new = new_secret(), new_secret()
    candidates = [
        Candidate(secret_id=1, secret_hash=hash_secret(new), is_usable=True),
        Candidate(secret_id=2, secret_hash=hash_secret(old), is_usable=True),
    ]
    assert verify_against(old, candidates) == 2
    assert verify_against(new, candidates) == 1


def test_iptal_edilmis_secret_ESLESMEZ_ama_YINE_DE_hashlenir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the hash for a revoked secret would make it
    distinguishable from a wrong one by timing alone."""
    calls = _count_verifications(monkeypatch)
    secret = new_secret()
    candidate = Candidate(secret_id=3, secret_hash=hash_secret(secret), is_usable=False)
    assert verify_against(secret, [candidate]) is None
    assert len(calls) == VERIFICATIONS_PER_ATTEMPT


# --- aile / scope turetmesi -------------------------------------------------


def test_scope_kumesi_AILE_KUMESINDEN_turer() -> None:
    """A scope with no family would be unreachable; a family with no scope
    would be silently open. Neither may happen, so the two are derived
    from one list rather than typed twice."""
    assert {s.value for s in ApiScope} == {scope_for(f) for f in DataFamily}


def test_usage_ailesi_veri_aileleri_arti_iki_yuzey() -> None:
    assert {u.value for u in UsageFamily} == {f.value for f in DataFamily} | set(
        EXTRA_USAGE_FAMILIES
    )
