"""Usage measurement.

Counters accumulate in Redis and are flushed to `api_usage_daily` by
`yfin api usage flush`. Writing a row per request would put a hot row in
the request path for data nobody reads in real time.

Billing is a separate subsystem and is not in this scope, but the
measurement is here from day one: the alternative is to start counting on
the day billing arrives and have no history to bill against or reason
about.

`estimated` marks a day whose counters could not be measured exactly --
Redis was unreachable and the limiter failed open. Billing has to be able
to tell a measured row from a reconstructed one instead of quietly
treating both as fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from yfin.api.core.config import ApiSettings
from yfin.api.models.clients import ApiClient
from yfin.api.models.plans import ApiUsageDaily
from yfin.api.ratelimit.connection import get_redis
from yfin.core.logging_setup import get_logger

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
    """Moves buffered counters into the database.

    Counters are read and deleted in one pass per day. Today's bucket is
    skipped by default because it is still being written to; a flush that
    took it would race with in-flight requests and lose whatever landed
    between the read and the delete.
    """
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


def monthly_total(session: Session, client_id: str, month: date) -> int:
    """Measured usage for a month, from the database side.

    The authoritative number for anything that is not a live limit
    decision: Redis holds the working counter, this holds the record.
    """
    total = session.execute(
        select(func.coalesce(func.sum(ApiUsageDaily.request_count), 0)).where(
            ApiUsageDaily.client_id == client_id,
            cast(func.date_trunc("month", ApiUsageDaily.day), Date) == month.replace(day=1),
        )
    ).scalar_one()
    return int(total)
