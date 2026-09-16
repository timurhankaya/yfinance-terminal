"""Transactional outboxes and the relay that drains them into Kafka.

The dependency runs one way: `stream/` may use `outbox/`, `outbox/` knows
nothing about the stream. What differs per queue is declared in `spec.py`."""
