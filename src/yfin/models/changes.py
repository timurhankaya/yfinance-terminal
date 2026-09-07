"""The pipeline's change outbox and its relay cursor.

Sibling of the stream tables' outbox (`models/stream.py`) and deliberately
not the same table. Two queues, two relays, two advisory locks, two
offsets: a broker outage on one must not stall the other, and the tick
outbox's `id` walk is only correct because it has a single writer thread --
a premise the pipeline cannot make.

That difference is the reason for `xid`. Symbol transactions commit
concurrently, N shard processes by worker threads, so `id` order is not
commit order: an `id`-ordered walk would step past the rows of a
transaction that took its ids early and committed late, and those rows
exist nowhere else. The relay therefore walks `(xid, id)` and only reads
rows whose `xid` is below `pg_snapshot_xmin(pg_current_snapshot())`, i.e.
transactions that have certainly ended.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import Base, RawJsonType, TsType, Xid8Type

#: Advisory lock for `yfin changes relay`. Declared next to the tables it
#: protects; `outbox/spec.py` carries the literal it hands the relay, and
#: a unit test holds the two together.
PIPELINE_RELAY_LOCK_NAME = "yfin_pipeline_relay"

#: Width of the `family` column: the longest `DataFamily` value is
#: "fundamentals" at twelve characters.
FAMILY_LENGTH = 16


class PipelineOutbox(Base):
    """One row-level change event, queued inside the writing transaction.

    Atomic with the data by construction: the flush is the last statement
    before the commit, so a row is in the queue if and only if the write it
    describes is in the database. A failed commit loses both.

    A hypertable with one-hour chunks, dropped chunk by chunk as the relay
    publishes them. The design gives it no retention beyond delivery: the
    read API is how a consumer fetches current state, and this is only how
    it learns that state moved.
    """

    __tablename__ = "pipeline_outbox"
    # `(xid, id)` is the relay's walk order and its cursor, so the index
    # carries both. No index on `created_at`: the hypertable partitions on
    # it, and the only query that reads it is the cleanup cutoff's `min()`.
    __table_args__ = (Index("ix_pipeline_outbox_xid", "xid", "id"),)

    # Row identity, and the consumer's dedupe key -- it travels as the
    # `yfin-outbox-id` header. `(table, key, occurred_at)` would not do:
    # one transaction can write the same row twice (`symbols` from three
    # datasets, `news` from two) under a single flush timestamp.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)

    # The hypertable's time column. Taken from one `clock_timestamp()` per
    # flush, so the database clock orders events across shard processes and
    # hosts rather than each host's own clock.
    created_at: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # The writing transaction. `pg_current_xact_id()` returns the top-level
    # id -- savepoints do not change it -- and it is already assigned by
    # the data writes that precede the flush, so the default costs nothing.
    xid: Mapped[int] = mapped_column(
        Xid8Type(), nullable=False, server_default=text("pg_current_xact_id()")
    )

    # Picks the topic: one of the seven `DataFamily` values. Not an ENUM,
    # for the reason `BarIntervalType` gives -- the valid set lives in
    # `core/families.py` and a second copy in the schema can drift.
    family: Mapped[str] = mapped_column(String(FAMILY_LENGTH, collation="C"), nullable=False)

    # The Kafka message key, and therefore the unit ordering holds within.
    # `Text` rather than a width: the routing map picks this per table and
    # it is a symbol on most, a `query_term` or a `news_id` on others.
    partition_key: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)

    payload: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class PipelineRelayOffset(Base):
    """How far the pipeline relay has published.

    Its own table rather than a second row in `stream_relay_offset`, so the
    two relays never contend for one row lock.

    The cursor is the PAIR `(last_published_xid, last_published_id)`. A
    single `xid` would stall the relay on any transaction larger than one
    batch: it could neither advance past it nor resume inside it. With the
    pair, a large transaction is drained across passes.
    """

    __tablename__ = "pipeline_relay_offset"
    __table_args__ = (CheckConstraint("id = 1", name="ck_pipeline_relay_offset_id"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_published_xid: Mapped[int] = mapped_column(
        Xid8Type(), nullable=False, server_default=text("'0'::xid8")
    )
    last_published_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


def changes_timescale_ddl() -> tuple[str, ...]:
    """Hypertable DDL for the change outbox.

    Its own function, next to `timescale_ddl()` and `stream_timescale_ddl()`
    and for the same reason: the migrations that ran before this table
    existed call those, and merging would make a past migration fail
    against a schema where `pipeline_outbox` is not there yet.

    One hour, like `stream_outbox`. This is a queue, not an archive -- the
    relay drops chunks as it publishes them, so the interval decides how
    promptly the space comes back. `create_default_indexes => FALSE`
    because the default DESC index on the time column is absent from
    `Base.metadata`, and autogenerate would report it as a deletion
    forever.
    """
    return (
        "SELECT create_hypertable('pipeline_outbox', "
        "by_range('created_at', INTERVAL '1 hour'), "
        "create_default_indexes => FALSE)",
    )


__all__ = [
    "FAMILY_LENGTH",
    "PIPELINE_RELAY_LOCK_NAME",
    "PipelineOutbox",
    "PipelineRelayOffset",
    "changes_timescale_ddl",
]
