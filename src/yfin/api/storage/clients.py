"""Client repository: the single write gate for API credentials. Every
operation that changes what a token may do bumps `auth_epoch`, which is
what makes revocation immediate."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.api.auth.hashing import (
    VERIFICATIONS_PER_ATTEMPT,
    Candidate,
    hash_secret,
    new_client_id,
    new_secret,
)
from yfin.api.models.clients import ApiClient, ApiClientScope, ApiClientSecret
from yfin.api.models.plans import ApiPlan

#: How long a rotated-out secret keeps working. Long enough for a client
#: to deploy the new one on its own schedule, short enough that a
#: forgotten rotation does not leave two live secrets forever.
DEFAULT_ROTATION_GRACE = timedelta(days=7)

#: A client may hold at most this many live secrets. Enforced here: no
#: unique index can express a per-parent count.
MAX_LIVE_SECRETS = VERIFICATIONS_PER_ATTEMPT


class ClientError(Exception):
    """Refusals an operator should see, not stack traces."""


class UnknownClient(ClientError):
    pass


class UnknownPlan(ClientError):
    pass


class TooManyLiveSecrets(ClientError):
    pass


@dataclass(frozen=True)
class CreatedClient:
    """The one moment the secret exists in the clear."""

    client_id: str
    secret: str


def _now() -> datetime:
    return datetime.now(UTC)


def _get(session: Session, client_id: str) -> ApiClient:
    client = session.get(ApiClient, client_id)
    if client is None:
        raise UnknownClient(client_id)
    return client


def live_secrets(session: Session, client_id: str) -> list[ApiClientSecret]:
    """Secrets that could still authenticate: not revoked, not expired."""
    now = _now()
    rows = session.scalars(
        select(ApiClientSecret).where(ApiClientSecret.client_id == client_id)
    ).all()
    return [
        row
        for row in rows
        if row.revoked_at is None and (row.expires_at is None or row.expires_at > now)
    ]


def candidates_for(session: Session, client_id: str) -> list[Candidate]:
    """Everything the verifier needs, including revoked and expired secrets
    flagged unusable, so a revoked secret is not distinguishable from a
    wrong one by timing."""
    now = _now()
    rows = session.scalars(
        select(ApiClientSecret)
        .where(ApiClientSecret.client_id == client_id)
        .order_by(ApiClientSecret.created_at.desc())
        .limit(MAX_LIVE_SECRETS)
    ).all()
    return [
        Candidate(
            secret_id=row.id,
            secret_hash=row.secret_hash,
            is_usable=row.revoked_at is None
            and (row.expires_at is None or row.expires_at > now),
        )
        for row in rows
    ]


def create_client(
    session: Session,
    *,
    name: str,
    owner_email: str,
    plan: str,
    scopes: Sequence[str],
) -> CreatedClient:
    if session.get(ApiPlan, plan) is None:
        raise UnknownPlan(plan)

    secret = new_secret()
    client = ApiClient(
        client_id=new_client_id(),
        name=name,
        owner_email=owner_email,
        plan=plan,
    )
    session.add(client)
    session.flush()

    session.add(ApiClientSecret(client_id=client.client_id, secret_hash=hash_secret(secret)))
    for scope in dict.fromkeys(scopes):
        session.add(ApiClientScope(client_id=client.client_id, scope=scope))
    session.flush()

    return CreatedClient(client_id=client.client_id, secret=secret)


def rotate_secret(
    session: Session,
    client_id: str,
    *,
    grace: timedelta = DEFAULT_ROTATION_GRACE,
) -> str:
    """Issues a new secret and puts the current one on a deadline. Refuses
    when two are already live: that is an unfinished rotation."""
    client = _get(session, client_id)
    existing = live_secrets(session, client_id)
    if len(existing) >= MAX_LIVE_SECRETS:
        raise TooManyLiveSecrets(client_id)

    secret = new_secret()
    session.add(ApiClientSecret(client_id=client_id, secret_hash=hash_secret(secret)))

    deadline = _now() + grace
    for row in existing:
        if row.expires_at is None or row.expires_at > deadline:
            row.expires_at = deadline

    # A rotation is not itself a revocation, but the old secret now has an
    # end date; bumping the epoch keeps the client's token state honest.
    client.auth_epoch += 1
    session.flush()
    return secret


def revoke_secret(session: Session, client_id: str, secret_id: int) -> None:
    """Kills one secret now. Tokens minted with it stop working."""
    client = _get(session, client_id)
    row = session.get(ApiClientSecret, secret_id)
    if row is None or row.client_id != client_id:
        raise UnknownClient(f"{client_id}/{secret_id}")
    if row.revoked_at is None:
        row.revoked_at = _now()
        client.auth_epoch += 1
        session.flush()


def set_scopes(session: Session, client_id: str, scopes: Sequence[str]) -> None:
    client = _get(session, client_id)
    session.query(ApiClientScope).filter(ApiClientScope.client_id == client_id).delete()
    for scope in dict.fromkeys(scopes):
        session.add(ApiClientScope(client_id=client_id, scope=scope))
    # Narrowing a scope must take effect now, not when the token expires.
    client.auth_epoch += 1
    session.flush()


def set_plan(session: Session, client_id: str, plan: str) -> None:
    client = _get(session, client_id)
    if session.get(ApiPlan, plan) is None:
        raise UnknownPlan(plan)
    client.plan = plan
    # A downgrade has to bite immediately, or a client keeps the old
    # limits for the life of an already-issued token.
    client.auth_epoch += 1
    session.flush()


def set_active(session: Session, client_id: str, active: bool) -> None:
    client = _get(session, client_id)
    client.is_active = active
    client.disabled_at = None if active else _now()
    client.auth_epoch += 1
    session.flush()


@dataclass(frozen=True)
class AuthRecord:
    """Everything the token endpoint needs, in one read."""

    client_id: str
    is_active: bool
    auth_epoch: int
    scopes: tuple[str, ...]
    candidates: list[Candidate]


def load_for_auth(session: Session, client_id: str) -> AuthRecord | None:
    """Loads a client for authentication, or None if there is no such id;
    the caller must still do the same hashing work for an unknown client."""
    client = session.get(ApiClient, client_id)
    if client is None:
        return None
    return AuthRecord(
        client_id=client.client_id,
        is_active=client.is_active,
        auth_epoch=client.auth_epoch,
        scopes=tuple(scopes_of(session, client_id)),
        candidates=candidates_for(session, client_id),
    )


def epoch_of(session: Session, client_id: str) -> int:
    """The client's current authorisation epoch.

    Callers publish this to Redis after committing, which is what makes a
    revocation effective before the token expires.
    """
    return _get(session, client_id).auth_epoch


def scopes_of(session: Session, client_id: str) -> list[str]:
    return sorted(
        session.scalars(
            select(ApiClientScope.scope).where(ApiClientScope.client_id == client_id)
        ).all()
    )


def list_clients(session: Session) -> list[ApiClient]:
    return list(session.scalars(select(ApiClient).order_by(ApiClient.created_at)).all())
