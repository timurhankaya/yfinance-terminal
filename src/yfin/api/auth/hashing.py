"""Client secret hashing. Parameters are deliberately light: the secret is a
random 256-bit value, so the hash only guards a leaked database, and the
token endpoint is callable unauthenticated. Every verification runs the
same number of hashings and checks revocation/expiry only afterwards, so
timing cannot reveal whether a client or secret exists."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from yfin.api.models.clients import CLIENT_ID_PREFIX

#: OWASP's minimum argon2id profile. Pinned here and asserted in tests:
#: if these drift, the decoy hash below no longer costs the same as a
#: real one and the timing signal comes straight back.
TIME_COST = 2
MEMORY_COST_KIB = 19456  # 19 MiB
PARALLELISM = 1
HASH_LENGTH = 32
SALT_LENGTH = 16

#: 32 bytes -> 43 URL-safe characters.
SECRET_BYTES = 32
#: 24 bytes -> 32 characters, plus the 4-character prefix.
CLIENT_ID_BYTES = 24

_hasher = PasswordHasher(
    time_cost=TIME_COST,
    memory_cost=MEMORY_COST_KIB,
    parallelism=PARALLELISM,
    hash_len=HASH_LENGTH,
    salt_len=SALT_LENGTH,
)

#: A real hash of a value nobody holds, used to pad the verification path
#: to a fixed number of hashings. Built with the same hasher, so it costs
#: exactly what a genuine comparison costs.
_DECOY_HASH = _hasher.hash(secrets.token_urlsafe(SECRET_BYTES))

#: How many verifications every authentication attempt performs.
VERIFICATIONS_PER_ATTEMPT = 2

#: Concurrent hashings allowed in this process. Each one reserves
#: `MEMORY_COST_KIB`, so without a cap the memory ceiling would be set by
#: the thread pool size -- that is, by an attacker's request rate.
MAX_CONCURRENT_HASHINGS = 8
_hash_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASHINGS)


def new_client_id() -> str:
    """`yfc_` + 32 URL-safe characters.

    The prefix is not decoration: it makes a leaked string recognisable
    in a log or a paste, which is what lets someone act on the leak.
    """
    return CLIENT_ID_PREFIX + secrets.token_urlsafe(CLIENT_ID_BYTES)


def new_secret() -> str:
    """256 bits. Shown once, never stored in the clear."""
    return secrets.token_urlsafe(SECRET_BYTES)


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(secret: str, secret_hash: str) -> bool:
    """False for every failure, including a malformed stored hash
    (`InvalidHashError` derives from ValueError, not `VerificationError`)."""
    with _hash_slots:
        try:
            return _hasher.verify(secret_hash, secret)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


@dataclass(frozen=True)
class Candidate:
    """A stored secret to check the presented one against."""

    secret_id: int
    secret_hash: str
    is_usable: bool  # not revoked and not expired


def verify_against(secret: str, candidates: list[Candidate]) -> int | None:
    """Returns the id of the matching usable secret, or None. Always performs
    `VERIFICATIONS_PER_ATTEMPT` hashings (padded with the decoy) and applies
    usability to the result rather than skipping work, so a revoked secret
    is not distinguishable by timing from a wrong one."""
    padded = list(candidates[:VERIFICATIONS_PER_ATTEMPT])
    matched: int | None = None

    for index in range(VERIFICATIONS_PER_ATTEMPT):
        if index < len(padded):
            candidate = padded[index]
            if verify_secret(secret, candidate.secret_hash) and candidate.is_usable:
                matched = candidate.secret_id
        else:
            # Result discarded; the work is the point.
            verify_secret(secret, _DECOY_HASH)

    return matched
