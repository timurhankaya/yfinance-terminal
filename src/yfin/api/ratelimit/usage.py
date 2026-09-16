"""Usage measurement. Counters accumulate in Redis and are flushed to
`api_usage_daily` by `yfin api usage flush`, keeping a hot row out of the
request path. `estimated` marks a day the limiter failed open, so billing
can tell a measured row from a reconstructed one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from yfin.api.core.config import ApiSettings
from yfin.api.models.clients import ApiClient
from yfin.api.models.plans import ApiUsageDaily
from yfin.api.ratelimit.connection import get_redis
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc

log = get_logger(__name__)

USAGE_KEY = "usage:{day}"
ESTIMATED_KEY = "usage_estimated:{day}"
LAST_USED_KEY = "last_used"

#: Long enough that a missed flush does not lose a day's counting, short
#: enough that abandoned keys do not accumulate.
USAGE_TTL_SECONDS = 7 * 24 * 3600


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def record(
    settings: ApiSettings,
    *,
    client_id: str,
    family: str,
    estimated: bool = False,
) -> None:
    """Counts one billable request. Never raises into the request path."""
    day = _today()
    field = f"{client_id}:{family}"
    try:
        redis = get_redis(settings)
        pipe = redis.pipeline()
        pipe.hincrby(USAGE_KEY.format(day=day), field, 1)
        pipe.expire(USAGE_KEY.format(day=day), USAGE_TTL_SECONDS)
        if estimated:
            pipe.sadd(ESTIMATED_KEY.format(day=day), field)
            pipe.expire(ESTIMATED_KEY.format(day=day), USAGE_TTL_SECONDS)
        # Last use is buffered, not written per request: an UPDATE on
        # every call would make api_clients a hot row.
        pipe.hset(LAST_USED_KEY, client_id, datetime.now(UTC).isoformat())
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a request
        log.error("usage_not_recorded", client_id=client_id, error=str(exc))
        # The request is served either way; what is lost is the count, and
        # the day's row is marked estimated because of exactly this.
        inc("yfin_api_redis_failopen_total", where="usage")


@dataclass(frozen=True)
class FlushResult:
    usage_rows: int
    last_used_rows: int
    days: tuple[str, ...]


def _pending_days(redis_client: object, keep_today: bool) -> list[str]:
    days = []
    for key in redis_client.scan_iter(match="usage:*"):  # type: ignore[attr-defined]
        day = str(key).split(":", 1)[1]
        if keep_today and day == _today():
            continue
        days.append(day)
    return sorted(days)


def flush(session: Session, settings: ApiSettings, *, include_today: bool = False) -> FlushResult:
    """Moves buffered counters into the database. Today's bucket is skipped
    by default: it is still being written to, and a flush would lose what
    lands between the read and the delete."""
    redis = get_redis(settings)
    days = _pending_days(redis, keep_today=not include_today)

    usage_rows = 0
    for day in days:
        key = USAGE_KEY.format(day=day)
        counts = redis.hgetall(key)
        estimated_fields = set(redis.smembers(ESTIMATED_KEY.format(day=day)) or [])
        if not counts:
            redis.delete(key, ESTIMATED_KEY.format(day=day))
            continue

        rows = []
        for field, count in counts.items():
            client_id, _, family = str(field).partition(":")
            if not family:
                continue
            rows.append(
                {
                    "client_id": client_id,
                    "day": date.fromisoformat(day),
                    "endpoint_family": family,
                    "request_count": int(count),
                    "estimated": field in estimated_fields,
                }
            )

        if rows:
            statement = pg_insert(ApiUsageDaily).values(rows)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["client_id", "day", "endpoint_family"],
                    set_={
                        # Additive: a second flush of the same day must add
                        # to what is there, not replace it.
                        "request_count": ApiUsageDaily.request_count
                        + statement.excluded.request_count,
                        "estimated": ApiUsageDaily.estimated
                        | statement.excluded.estimated,
                    },
                )
            )
            usage_rows += len(rows)

        redis.delete(key, ESTIMATED_KEY.format(day=day))

    last_used = redis.hgetall(LAST_USED_KEY) or {}
    for buffered_id, seen in last_used.items():
        session.execute(
            update(ApiClient)
            .where(ApiClient.client_id == str(buffered_id))
            .values(last_used_at=datetime.fromisoformat(str(seen)))
        )
    if last_used:
        redis.delete(LAST_USED_KEY)

    return FlushResult(
        usage_rows=usage_rows, last_used_rows=len(last_used), days=tuple(days)
    )


