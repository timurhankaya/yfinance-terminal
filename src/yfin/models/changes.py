"""The pipeline's change outbox and its relay cursor.

Separate from the stream outbox so a broker outage on one never stalls the
other. Symbol transactions commit concurrently, so `id` order is not commit
order; the relay walks `(xid, id)` below `pg_snapshot_xmin(pg_current_snapshot())`."""

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

    The flush is the last statement before the commit, so a row is queued
    iff the write it describes is in the database. One-hour chunks, dropped
    as the relay publishes them: no retention beyond delivery."""

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

    Its own table so the two relays never contend for one row lock. The
    cursor is the pair `(last_published_xid, last_published_id)`: a single
    `xid` could neither advance past nor resume inside a multi-batch txn."""

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

    Its own function so past migrations stay pinned to the tables they
    created. One-hour chunks: this is a queue, not an archive. The default
    DESC index is absent from `Base.metadata`, hence `create_default_indexes => FALSE`."""
    return (
        "SELECT create_hypertable('pipeline_outbox', "
        "by_range('created_at', INTERVAL '1 hour'), "
        "create_default_indexes => FALSE)",
    )
