"""Making an authorisation change effective before the token expires.

Token verification deliberately does not read the database -- that is
what makes it cheap. The cost is that a leaked secret, a narrowed scope
or a downgraded plan would otherwise stay in force until the token runs
out.

`auth_epoch` closes that gap. Every change bumps the counter in the
database and publishes it here; tokens carry the value they were minted
with, and the Bearer dependency (which already talks to Redis for rate
limiting) rejects anything that has fallen behind. No extra round trip,
no extra query.

Keys expire after one token lifetime because that is exactly how long
they can still matter: no token minted before the bump can outlive it.
"""

from __future__ import annotations

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis

EPOCH_KEY = "client_epoch:{client_id}"
DISABLED_KEY = "client_disabled:{client_id}"
REVOKED_SECRET_KEY = "revoked_secret:{secret_id}"


def epoch_key(client_id: str) -> str:
    return EPOCH_KEY.format(client_id=client_id)


def disabled_key(client_id: str) -> str:
    return DISABLED_KEY.format(client_id=client_id)


def revoked_secret_key(secret_id: int) -> str:
    return REVOKED_SECRET_KEY.format(secret_id=secret_id)


def publish_revocation(
    settings: ApiSettings,
    client_id: str,
    *,
    epoch: int,
    disabled: bool | None = None,
    revoked_secret_ids: tuple[int, ...] = (),
) -> None:
    """Publishes an authorisation change. Raises if Redis is unreachable.

    Raising is the point: the caller has committed a database change and
    has to be able to tell the operator that the change is not yet in
    force. Swallowing the error here would turn "this client is cut off"
    into a claim nobody verified.
    """
    ttl = settings.token_ttl_seconds
    client = get_redis(settings)
    pipe = client.pipeline()
    pipe.set(epoch_key(client_id), epoch, ex=ttl)
    if disabled is True:
        pipe.set(disabled_key(client_id), "1", ex=ttl)
    elif disabled is False:
        # Re-enabling must clear the flag, not wait out its TTL.
        pipe.delete(disabled_key(client_id))
    for secret_id in revoked_secret_ids:
        pipe.set(revoked_secret_key(secret_id), "1", ex=ttl)
    pipe.execute()
