"""What tells one outbox from another.

The relay's correctness argument -- publish, wait for every
acknowledgement, only then advance -- is the same whatever is queued. What
differs between the tick outbox and the pipeline's change outbox is
vocabulary: which table, which column picks the topic, whether that value
is an exchange code (upper-cased) or a family name (already lower case),
which advisory lock keeps a second relay out, and whether the consumer
needs a dedupe header.

Collecting that in one frozen value means the relay reads it rather than
branching on which outbox it is looking at, and it means a second outbox
costs a declaration rather than a fork of the process that has already
been reasoned about.

The lock name is a literal here rather than an import from
`stream/repository.py`: `outbox/` is the generic side and may not depend
on the stream package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class OutboxSpec:
    """One queue, and how the relay reads and routes it."""

    #: The queue table.
    table: str
    #: Its single-row cursor table.
    offset_table: str
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
    lock_name="yfin_stream_relay",
    route_column="exchange",
    key_column="symbol",
    topic_pattern="yfin.ticks.{exchange}",
    placeholder="{exchange}",
    upper_case_route=True,
    client_id="yfin-stream-relay",
    id_header=False,
)


__all__ = ["TICK_OUTBOX", "OutboxSpec"]
