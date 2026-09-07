"""Transactional outboxes and the relay that drains them into Kafka.

Extracted from `stream/` when a second queue needed the same machinery. The
relay's argument -- publish, wait for every acknowledgement, only then
advance the offset -- is about delivery, not about ticks, and repeating it
for a second outbox would have meant two copies of the one piece of code
whose failure mode is silent data loss.

The dependency runs one way: `stream/` may use `outbox/`, `outbox/` knows
nothing about the stream. What differs per queue is declared in
`spec.py` and read, not branched on.
"""

from __future__ import annotations

from yfin.outbox.spec import TICK_OUTBOX, OutboxSpec

__all__ = ["TICK_OUTBOX", "OutboxSpec"]
