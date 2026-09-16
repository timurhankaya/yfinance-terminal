"""Live WebSocket stream: tick archive, last-value table and its audit.

The column set of `live_ticks` and `live_quotes` is the field mapping in
`stream/protocol.py`; test_stream_schema checks the two against each other."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    FactValueType,
    PriceType,
    RawJsonType,
    ShortHashType,
    SymbolType,
    TsType,
    symbol_fk_column,
)


class TickFields:
    """The wire fields, shared by `live_ticks` and `live_quotes`.

    A mixin so the two tables cannot drift. `symbol` and the key columns
    are not here: they differ between the tables, and a foreign key cannot
    be shared through a mixin without `declared_attr` indirection."""

    # --- proto float (binary32) -> NUMERIC(28,12) --------------------------
    #
    # Stored through protocol.f32_decimal, never from the raw float:
    # protobuf widens binary32 to binary64, so an unconverted 232.35 would
    # be persisted as 232.35000610351562 forever.
    price: Mapped[Decimal | None] = mapped_column(PriceType())
    change_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    day_high: Mapped[Decimal | None] = mapped_column(PriceType())
    day_low: Mapped[Decimal | None] = mapped_column(PriceType())
    change: Mapped[Decimal | None] = mapped_column(PriceType())
    open_price: Mapped[Decimal | None] = mapped_column(PriceType())
    previous_close: Mapped[Decimal | None] = mapped_column(PriceType())
    strike_price: Mapped[Decimal | None] = mapped_column(PriceType())
    bid: Mapped[Decimal | None] = mapped_column(PriceType())
    ask: Mapped[Decimal | None] = mapped_column(PriceType())

    # --- proto sint64 -> BIGINT -------------------------------------------
    day_volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    last_size: Mapped[int | None] = mapped_column(BigInteger)
    bid_size: Mapped[int | None] = mapped_column(BigInteger)
    ask_size: Mapped[int | None] = mapped_column(BigInteger)
    vol_24hr: Mapped[int | None] = mapped_column(BigInteger)
    vol_all_currencies: Mapped[int | None] = mapped_column(BigInteger)

    # --- proto sint64 that is really a small code -> INTEGER ---------------
    #
    # INTEGER rather than SMALLINT: they are sint64 on the wire, and one
    # out-of-range value would abort the whole COPY batch with a DataError.
    options_type_code: Mapped[int | None] = mapped_column(Integer)
    mini_option_code: Mapped[int | None] = mapped_column(Integer)
    price_hint_code: Mapped[int | None] = mapped_column(Integer)

    # --- Yahoo enum codes, NOT NULL ---------------------------------------
    #
    # Exception to "absent -> NULL": proto3 cannot distinguish 0 from unset,
    # and market_hours 0 is PRE_MARKET, needed for price_bars.is_extended.
    # Not an ENUM type: a new Yahoo code must be stored, not rejected.
    quote_type_code: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    market_hours_code: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )

    # --- proto string -> VARCHAR ------------------------------------------
    #
    # Widths match `symbols`: currency/exchange 32, short_name 128. A
    # mismatch would break a future join, and the decoder truncates to
    # exactly these numbers.
    currency: Mapped[str | None] = mapped_column(String(32, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    short_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    from_currency: Mapped[str | None] = mapped_column(String(32, collation="C"))
    last_market: Mapped[str | None] = mapped_column(String(64, collation="C"))

    # --- proto double -> NUMERIC ------------------------------------------
    #
    # No f32 correction: these arrive as binary64 already. market_cap gets
    # BigNumType for the same reason ticker_info does -- NUMERIC(28,12)
    # overflows on a trillion-dollar cap.
    circulating_supply: Mapped[Decimal | None] = mapped_column(FactValueType())
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())

    # --- fields that need their own treatment ------------------------------

    # No FK: an option's underlying may legitimately be outside the
    # universe, and refusing the tick over it would discard real data.
    # SymbolType() all the same, so a future join cannot break on width.
    underlying_symbol: Mapped[str | None] = mapped_column(SymbolType())

    # SECONDS since epoch on the wire, unlike `time` which is
    # milliseconds. Reading it in the wrong unit puts every option expiry
    # in 1970, so the decoder range-checks it.
    expire_date_utc: Mapped[datetime | None] = mapped_column(TsType())

    # Our own clock at decode time. `received_at - ts_utc` is the
    # end-to-end lag and the only honest health signal for the stream --
    # message counts say nothing about whether we are falling behind.
    received_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)

    # Proto fields this build does not map yet. Normally NULL, so it costs
    # nothing; when it is not NULL it is the only evidence that
    # pricing.proto moved. RawJsonType (TEXT), not JSON: jsonb reorders
    # keys and payload_hash could no longer be recomputed.
    unknown_fields: Mapped[str | None] = mapped_column(RawJsonType())


class LiveTick(TickFields, Base):
    """Every message, kept.

    The PK triple: Yahoo can send several messages for one symbol within a
    millisecond, so (symbol, ts_utc) collides; and it re-sends identical
    snapshots, which `payload_hash` makes a no-op via ON CONFLICT DO NOTHING."""

    __tablename__ = "live_ticks"
    __table_args__ = (Index("ix_live_ticks_received_at", "received_at"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # SHA-256 over the mapped fields AND unknown_fields, first 16 hex
    # digits. Including unknown_fields is not cosmetic: without it, a
    # message whose 33 known fields match an earlier one but which carries
    # a NEW proto field would be dropped by ON CONFLICT DO NOTHING -- and
    # catching exactly that case is why unknown_fields exists.
    payload_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)


class LiveQuote(TickFields, Base):
    """The latest tick per symbol.

    Fed from the supervisor's last-value box rather than the writer queue,
    so it stays current even when the queue overflows. `yfin stream scope
    disable` deletes the row, or the table would only ever grow."""

    __tablename__ = "live_quotes"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)

    # Not a key here, but the guard column: the upsert only applies when
    # the incoming ts_utc is newer. Out-of-order delivery must never roll
    # the row back, and a per-column GREATEST would not do -- it would
    # blend two different instants into a state that never existed.
    ts_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    payload_hash: Mapped[str] = mapped_column(ShortHashType(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class StreamScope(Base):
    """Which symbols are streamed, and which are archived.

    Data, not configuration, like `intraday_scope`. Unlike there, this is
    a plain set: no row always means out of scope, so disable may delete."""

    __tablename__ = "stream_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # False keeps the symbol on the wire and in live_quotes but out of
    # live_ticks: some symbols are wanted live without their tick history.
    archive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class StreamOutbox(Base):
    """Transactional outbox for the optional Kafka publish path.

    Written in the same transaction as the tick, and only when Kafka is
    enabled. A hypertable: the relay drops whole chunks once published,
    since DELETE plus autovacuum could not keep up."""

    __tablename__ = "stream_outbox"
    __table_args__ = (Index("ix_stream_outbox_id", "id"),)

    # Ordering, not identity: the relay walks `id > last_published_id`.
    # That is only correct because a single writer thread commits batches
    # in order -- see the design's §8.1.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)

    # Generated by the writer, not copied from the tick's received_at. If
    # a late tick could carry a low created_at with a high id, it would
    # land in a chunk the relay already considers published and be dropped
    # unsent.
    created_at: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # No FK, and for a different reason than stream_rejects: everything
    # here has already passed the known-symbol filter, so unknown symbols
    # are not the issue. The issue is cost -- every insert would take a
    # shared lock on the `symbols` row, and a transient publish queue does
    # not need to pay what the archive pays for integrity.
    symbol: Mapped[str] = mapped_column(SymbolType(), nullable=False)

    # From `symbols.exchange`, the normalised source -- never the tick's
    # own raw exchange field. Raw values differing in case would split one
    # Kafka topic into two and break per-symbol ordering.
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    payload: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class StreamRelayOffset(Base):
    """How far the relay has published. One row, enforced; the real
    protection against two relay processes is the `yfin_stream_relay`
    advisory lock."""

    __tablename__ = "stream_relay_offset"
    __table_args__ = (CheckConstraint("id = 1", name="ck_stream_relay_offset_id"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_published_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class StreamRejectReason(enum.StrEnum):
    """Why a message, a field or a subscription entry was dropped.

    Mirrors the constants in `stream/protocol.py`; a test keeps the two in
    step. NON_FINITE_FIELD and FIELD_OUT_OF_RANGE null one column, not the row."""

    DECODE_FAILED = "decode_failed"
    NO_TIMESTAMP = "no_timestamp"
    UNKNOWN_SYMBOL = "unknown_symbol"
    SYMBOL_TOO_LONG = "symbol_too_long"
    NON_FINITE_FIELD = "non_finite_field"
    FIELD_OUT_OF_RANGE = "field_out_of_range"
    EXPIRE_DATE_RANGE = "expire_date_range"
    MALFORMED_SUBSCRIPTION = "malformed_subscription"


class StreamReject(Base):
    """Permanent record of what was dropped.

    Sampled per (symbol, reason) so a single broken feed cannot fill the
    table; exact totals live on stream_sessions. `queue_overflow` is not a
    reason here: a row per dropped tick would add load when already behind."""

    __tablename__ = "stream_rejects"
    __table_args__ = (Index("ix_stream_rejects_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)

    # No FK, and here it is mandatory: an `unknown_symbol` reject is by
    # definition about a symbol that is not in `symbols`. With a foreign
    # key the record of the rejection would itself be rejected. Same
    # reasoning as sync_run_items.symbol.
    symbol: Mapped[str | None] = mapped_column(SymbolType())
    reason: Mapped[StreamRejectReason] = mapped_column(
        Enum(
            StreamRejectReason,
            values_callable=lambda e: [m.value for m in e],
            name="stream_reject_reason",
        ),
        nullable=False,
    )
    detail: Mapped[str | None] = mapped_column(Text)

    # The wire bytes. live_ticks has no raw_json column, so for a message
    # that could not be decoded this is the only surviving evidence of
    # what actually arrived.
    raw_base64: Mapped[str | None] = mapped_column(Text)


class StreamStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class StreamSession(Base):
    """One run of `yfin stream run`; the streaming counterpart of sync_runs.

    Counters are accumulated in the writer thread and flushed with the
    batch commit. Updating them per message would double the write path.
    """

    __tablename__ = "stream_sessions"
    __table_args__ = (Index("ix_stream_sessions_started", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TsType())
    status: Mapped[StreamStatus] = mapped_column(
        Enum(
            StreamStatus,
            values_callable=lambda e: [m.value for m in e],
            name="stream_status",
        ),
        nullable=False,
    )
    connection_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    messages_received: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    # Ticks dropped because the queue was full. Not in stream_rejects on
    # purpose (see StreamReject) -- this counter is the whole record.
    rows_dropped: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class StreamConnectionHealth(Base):
    """Live state of one upstream connection, no history.

    After a hard kill rows would stay `open` forever, so a row whose
    `heartbeat_at` is older than two rescan intervals is stale, not healthy."""

    __tablename__ = "stream_connection_health"

    # `NMS` or `NMS#0` when an exchange spans several connections.
    connection_key: Mapped[str] = mapped_column(AsciiKeyType(40), primary_key=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stream_sessions.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(AsciiKeyType(16), nullable=False)

    # What we actually sent, including the canary. Yahoo silently truncates
    # a subscription past 100 symbols, so this is the number that has to be
    # checked against the quota -- not the size of the scope.
    subscribed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    connected_at: Mapped[datetime | None] = mapped_column(TsType())
    last_message_at: Mapped[datetime | None] = mapped_column(TsType())

    # The canary sits at the END of the subscription list, because Yahoo
    # truncates from the front. Silence here means the quota was exceeded
    # or the subscription was dropped -- and since the server never sends
    # an error, it is the only signal that either happened.
    last_canary_at: Mapped[datetime | None] = mapped_column(TsType())
    heartbeat_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


def stream_timescale_ddl() -> tuple[str, ...]:
    """Hypertable DDL for the stream tables, separate from bars'
    `timescale_ddl()` so past migrations stay pinned to their tables.
    `create_default_indexes => FALSE`: the default index is absent from
    Base.metadata. live_ticks: 1-day chunks keep the index working set in
    memory; stream_outbox: 1 hour, since it is a queue, not an archive."""
    return (
        "SELECT create_hypertable('live_ticks', "
        "by_range('ts_utc', INTERVAL '1 day'), "
        "create_default_indexes => FALSE)",
        "SELECT create_hypertable('stream_outbox', "
        "by_range('created_at', INTERVAL '1 hour'), "
        "create_default_indexes => FALSE)",
    )
