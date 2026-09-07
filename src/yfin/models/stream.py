"""Live WebSocket stream: tick archive, last-value table and its audit.

Eight tables. Measurements behind the shape:
docs/measurements/websocket.md

The column set of `live_ticks` and `live_quotes` is the 33-field mapping
in `stream/protocol.py`. The two are checked against each other by
test_stream_schema -- if they drifted, a decoded field would have nowhere
to land and the write would fail at runtime rather than at import.
"""

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
    """The 33 wire fields, shared by `live_ticks` and `live_quotes`.

    A mixin rather than two hand-written copies: the two tables are the
    same measurement seen twice (every tick, and the latest one), so a
    column that exists in only one of them is always a bug. Thirty-six
    columns written out twice would drift on the first schema change.

    `symbol` and the key columns are NOT here -- they differ between the
    two tables, and a foreign key cannot be shared through a mixin
    without `declared_attr` indirection that would obscure more than it
    saves.
    """

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
    # INTEGER rather than SMALLINT even though the observed values are
    # tiny: they are sint64 on the wire, and one out-of-range value in a
    # 500-row batch would abort the whole COPY with a DataError. The
    # decoder nulls anything outside INTEGER and records a reject, so the
    # batch survives either way -- but the wider column keeps that path
    # rare.
    options_type_code: Mapped[int | None] = mapped_column(Integer)
    mini_option_code: Mapped[int | None] = mapped_column(Integer)
    price_hint_code: Mapped[int | None] = mapped_column(Integer)

    # --- Yahoo enum codes, NOT NULL ---------------------------------------
    #
    # These two are the exception to the "absent -> NULL" rule, and the
    # exception is load-bearing. proto3 cannot distinguish 0 from unset,
    # and market_hours 0 is PRE_MARKET -- a real value. Nulling it would
    # make it impossible to derive price_bars.is_extended (NOT NULL) for
    # exactly the pre-market rows that need the flag.
    #
    # The code -> name mapping stays in protocol.py. Making these an ENUM
    # type here would create a second source of truth that drifts the
    # moment Yahoo adds a code, and a new code would then be rejected by
    # the database instead of stored.
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

    Primary key is a triple for two separate reasons, and dropping either
    one loses data:

      * Yahoo can send several messages for the same symbol inside one
        millisecond, so (symbol, ts_utc) collides and the second tick
        would vanish.
      * Yahoo re-sends identical snapshots. `payload_hash` makes those a
        no-op through ON CONFLICT DO NOTHING.

    So two different ticks in the same millisecond are both stored, and
    the same tick twice is stored once.
    """

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

    A derived view of live_ticks, not a second archive: it exists so a
    reader can answer "what is the price now" without scanning a
    hypertable. Fed from the supervisor's last-value box rather than the
    writer queue, so it stays current even when the queue overflows.

    Row count is not fixed: `yfin stream scope disable` deletes the row.
    Left in place, the table would only ever grow and `yfin stream status`
    would show quotes for symbols nobody streams any more.
    """

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

    Data, not configuration -- the same reasoning as `intraday_scope`: a
    subset of a 5,000-symbol universe does not fit in .env and needs to be
    versioned and mutable.

    The resolution rule differs from intraday_scope's, deliberately.
    There, "no row" means the whole universe for some intervals, which is
    why `bars scope disable` must keep the row. Here the table is a plain
    set: no row always means out of scope, so disable may delete.
    """

    __tablename__ = "stream_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # False keeps the symbol on the wire and in live_quotes but out of
    # live_ticks. This is the volume dial: the archive is ~500-600 GB per
    # year at 500 symbols, and some symbols are wanted live without their
    # tick history being wanted at all.
    archive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class StreamOutbox(Base):
    """Transactional outbox for the optional Kafka publish path.

    Written in the same transaction as the tick, so a row is in the outbox
    if and only if it is in the archive. Only written when Kafka is
    enabled: with it off this table stays empty and costs nothing.

    A hypertable, and that is what makes the cost bearable. The relay
    drops whole chunks once they are published; DELETE plus autovacuum
    could not keep up with a queue this size.
    """

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
    """How far the relay has published.

    One row, enforced. A second row would mean two relays not seeing each
    other's progress -- though the real protection against two relay
    processes is the `yfin_stream_relay` advisory lock, not this check.
    """

    __tablename__ = "stream_relay_offset"
    __table_args__ = (CheckConstraint("id = 1", name="ck_stream_relay_offset_id"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_published_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class StreamRejectReason(enum.StrEnum):
    """Why a message, a field or a subscription entry was dropped.

    Mirrors the constants in `stream/protocol.py`; a test keeps the two
    in step. Not every reason is fatal to the row -- NON_FINITE_FIELD and
    FIELD_OUT_OF_RANGE null one column and keep the other 32.
    """

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

    The same argument as bar_gaps: if the fact that a tick was dropped is
    not written down when it happens, it is gone -- nothing can
    reconstruct it later.

    Sampled per (symbol, reason) so a single broken feed cannot fill the
    table. Counts are NOT sampled: the exact total lives on
    stream_sessions. `queue_overflow` is deliberately absent from the
    reasons -- writing a row per dropped tick would add load at exactly
    the moment the writer is already behind.
    """

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
    """Live state of one upstream connection.

    Current state only, no history. `session_id` and `heartbeat_at` are
    what stop it from lying after a crash: killed hard, the rows would
    stay `open` forever and `yfin stream status` would report a dead
    process as healthy. A row whose heartbeat is older than two rescan
    intervals is stale, not healthy.
    """

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
    """Hypertable DDL for the stream tables.

    Separate from bars' `timescale_ddl()` rather than merged into it: the
    initial migration imports that function and runs it against a schema
    where these tables do not exist yet. Merging would make a past
    migration fail.

    `create_default_indexes => FALSE` for the same reason as price_bars:
    the default index is absent from Base.metadata, so autogenerate
    reports it as a deletion forever and the "empty diff" gate never
    opens.

    Chunk intervals differ by an order of magnitude on purpose:

      * live_ticks, 1 day. At ~5.8M rows/day a 7-day chunk (what
        price_bars uses) would reach ~40M rows and its index working set
        would not stay in memory.
      * stream_outbox, 1 hour. This one is a queue, not an archive: the
        relay drops chunks as it publishes them, so the interval sets how
        promptly space comes back.
    """
    return (
        "SELECT create_hypertable('live_ticks', "
        "by_range('ts_utc', INTERVAL '1 day'), "
        "create_default_indexes => FALSE)",
        "SELECT create_hypertable('stream_outbox', "
        "by_range('created_at', INTERVAL '1 hour'), "
        "create_default_indexes => FALSE)",
    )
