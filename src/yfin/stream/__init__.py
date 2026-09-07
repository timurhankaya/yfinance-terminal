"""Live WebSocket ingest: Yahoo's pricing stream into PostgreSQL.

Measurements: docs/measurements/websocket.md

Layer rule: writes go through `storage/contracts.py`, and every read and
maintenance query the *ingest* path needs lives in `repository.py` --
scope, sessions, connection health, quote cleanup. `relay.py` is the one
exception and it is deliberate: it runs as its own process
(`yfin stream relay`), and its offset and `drop_chunks` queries are
meaningless to the ingest path. Putting them in `repository.py` would
hand every stream module a vocabulary only the relay uses.

`writer.py` is the second exception, for a reason worth stating: its
`symbols` reads ask whether a row EXISTS (a foreign-key question), while
`repository.load_scope` asks whether a symbol is ELIGIBLE (`is_active`
plus the scope join). Same table, different questions -- reusing the
repository query there would be a bug, not a tidy-up. Its
`_exchange_lookup` additionally has to run on the batch session, which no
`repository.py` method can hand it.

`protocol.py` is the only module here with a yfinance import statement,
but that does not confine the dependency: `connection.py` needs its
decoder, `supervisor.py` needs `connection.py`, and `writer.py` needs the
supervisor, so importing the writer loads yfinance too. `rejects.py` is
the leaf that holds the `Reject` / `DecodeResult` vocabulary, so naming a
dropped tick costs nothing. `protocol.py` is likewise not free of the
database -- see its own docstring.
"""
