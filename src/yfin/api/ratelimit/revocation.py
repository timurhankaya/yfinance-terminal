"""Making an authorisation change effective before the token expires:
every change bumps `auth_epoch` and publishes it here, and the Bearer
dependency rejects tokens minted with an older value. Keys expire after
one token lifetime, which is exactly how long they can still matter."""

from __future__ import annotations

from datetime import UTC, datetime

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis
from yfin.core.logging_setup import get_logger

#: Written by the API at startup so `yfin api client disable` can refuse
#: to publish into a Redis no API process has ever used. The value is the
#: time of the last claim. No TTL: it must survive the API being down,
#: which is when a revocation is most likely to be issued.
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
    """Publishes an authorisation change. Raises if Redis is unreachable: the
    caller has committed a database change and must be able to tell the
    operator it is not yet in force."""
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
