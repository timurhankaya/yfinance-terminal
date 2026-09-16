"""What tells one outbox from another: table, topic column, lock name,
dedupe header. The relay reads the spec rather than branching on the queue.
The lock name is a literal, not an import from `stream/repository.py`,
because `outbox/` may not depend on the stream package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


@dataclass(frozen=True)
class OutboxSpec:
    """One queue, and how the relay reads and routes it."""

    #: The queue table.
    table: str
    #: Its single-row cursor table.
    offset_table: str
    #: How the relay walks the queue, and what the offset stores. `id` is
    #: correct only for a queue with a single writer thread; a queue written
    #: by concurrent transactions has to walk `(xid, id)` or it steps past
    #: the rows of a transaction that took its ids early and committed late.
    #: See `cursor.py`.
    cursor: Literal["id", "xid"]
    #: Advisory lock. Two relays on one outbox would publish the same rows
    #: and roll each other's progress back; the offset table's single-row
    #: constraint does not prevent that, only the lock does.
    lock_name: str
    #: The column whose value picks the topic.
    route_column: str
    #: The column whose value becomes the Kafka message key, and therefore
    #: the unit ordering is guaranteed within.
    key_column: str
    #: Topic name with `placeholder` standing in for the route value.
    topic_pattern: str
    #: The literal `topic_pattern` substitutes.
    placeholder: str
    #: Exchange codes arrive in mixed case and are normalised; family names
    #: are already the closed lower-case set and are left alone.
    upper_case_route: bool
    #: Distinguishes the two relays in the broker's own logs and metrics.
    client_id: str
    #: Whether to send the outbox row id as a `yfin-outbox-id` header. The
    #: change outbox needs it -- one transaction can write the same row
    #: twice, so `(table, key, occurred_at)` is not a dedupe key. Ticks are
    #: deduped on `live_ticks`' primary key, which travels in the payload,
    #: and their topics stay header-free.
    id_header: bool


#: The live stream's tick queue, exactly as it has always behaved.
TICK_OUTBOX: Final = OutboxSpec(
    table="stream_outbox",
    offset_table="stream_relay_offset",
    # A single writer thread, so `id` follows commit order -- the premise
    # `stream/writer.py` states and this depends on.
    cursor="id",
    lock_name="yfin_stream_relay",
    route_column="exchange",
    key_column="symbol",
    topic_pattern="yfin.ticks.{exchange}",
    placeholder="{exchange}",
    upper_case_route=True,
    client_id="yfin-stream-relay",
    id_header=False,
)


#: The pipeline's change queue. Written by every symbol, market and domain
#: transaction, which commit concurrently -- hence the `xid` walk.
CHANGES_OUTBOX: Final = OutboxSpec(
    table="pipeline_outbox",
    offset_table="pipeline_relay_offset",
    cursor="xid",
    lock_name="yfin_pipeline_relay",
    route_column="family",
    key_column="partition_key",
    topic_pattern="yfin.changes.{family}",
    placeholder="{family}",
    # `DataFamily` is already the closed lower-case set the seven
    # `<family>:read` scopes are named after; upper-casing it would name a
    # topic no ACL and no consumer expects.
    upper_case_route=False,
    client_id="yfin-changes-relay",
    # One transaction can write the same row twice -- `symbols` from three
    # datasets, `news` from two -- under one flush timestamp, so
    # `(table, key, occurred_at)` is not a dedupe key and the outbox id has
    # to travel.
    id_header=True,
)


__all__ = ["CHANGES_OUTBOX", "TICK_OUTBOX", "OutboxSpec"]
