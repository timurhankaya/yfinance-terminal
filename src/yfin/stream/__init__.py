"""Live WebSocket ingest: Yahoo's pricing stream into PostgreSQL.

Design: docs/superpowers/specs/2026-09-06-websocket-streaming-design.md
Measurements: docs/measurements/websocket.md

Layer rule: writes go through `storage/contracts.py`; every read and
maintenance query lives in `stream/repository.py`. `protocol.py` is the
only module here that imports yfinance, and it knows nothing about the
database.
"""
