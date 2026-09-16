"""Live WebSocket ingest: Yahoo's pricing stream into PostgreSQL.

Writes go through `storage/contracts.py`; ingest-path reads live in
`repository.py`. `stream/` may import `outbox/`, never the reverse.
"""
