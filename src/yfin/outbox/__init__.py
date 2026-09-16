"""Transactional outboxes and the relay that drains them into Kafka.

The dependency runs one way: `stream/` may use `outbox/`, `outbox/` knows
nothing about the stream. What differs per queue is declared in `spec.py`."""

from __future__ import annotations

from yfin.outbox.spec import TICK_OUTBOX, OutboxSpec

__all__ = ["TICK_OUTBOX", "OutboxSpec"]
