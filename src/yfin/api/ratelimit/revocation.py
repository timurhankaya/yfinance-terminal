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

from datetime import UTC, datetime

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis
from yfin.core.logging_setup import get_logger

#: Written by the API at startup. Its only job is to answer "has an API
#: process ever used this Redis?", and the reason it exists is a measured
#: failure: `yfin api client disable` published a revocation to the
#: operator's local Redis while the API read a different one. The command
#: reported success -- it HAD reached a Redis -- and the disabled client
#: kept working. An exit code that proves only "some Redis answered" is
#: exactly the false assurance this design set out to avoid.
#:
#: What it proves and what it does not: an API process has used this
#: Redis at some point. It does not prove the API you care about uses it
#: -- a developer running an instance against the default URL claims that
#: Redis too. The value is the time of the last claim, so an operator
#: staring at a failed propagation can tell "nothing has ever run here"
#: from "something ran here, months ago".
#:
#: No TTL: it must survive the API being down, which is when a revocation
#: is most likely to be issued.
MARKER_KEY = "yfin:api:redis"

EPOCH_KEY = "client_epoch:{client_id}"
DISABLED_KEY = "client_disabled:{client_id}"
REVOKED_SECRET_KEY = "revoked_secret:{secret_id}"

log = get_logger(__name__)


def epoch_key(client_id: str) -> str:
    return EPOCH_KEY.format(client_id=client_id)


def disabled_key(client_id: str) -> str:
    return DISABLED_KEY.format(client_id=client_id)


def revoked_secret_key(secret_id: int) -> str:
    return REVOKED_SECRET_KEY.format(secret_id=secret_id)


class WrongRedis(RuntimeError):
    """The Redis reached is not one an API has ever used."""


def mark_api_redis(settings: ApiSettings) -> None:
    """Claims this Redis as the API's. Best effort: a marker that cannot
    be written must not stop the API from serving."""
    try:
        get_redis(settings).set(MARKER_KEY, datetime.now(UTC).isoformat())
    except Exception as exc:  # noqa: BLE001 - never blocks startup
        log.error("redis_marker_not_written", error=str(exc))


def assert_api_redis(settings: ApiSettings) -> None:
    """Refuses to publish into a Redis no API has claimed."""
    if not get_redis(settings).exists(MARKER_KEY):
        raise WrongRedis(
            f"{settings.redis_url} has never been used by the API; "
            "check YFAPI_REDIS_URL points at the same instance the API reads"
        )



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
    # Before anything else: publishing into the wrong Redis is worse
    # than failing, because it looks like success.
    assert_api_redis(settings)
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
