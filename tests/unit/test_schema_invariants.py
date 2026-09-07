"""Invariants that must hold across the whole schema, not just one table.

These tests scan the ENTIRE `Base.metadata`, not individual tables. Defining
a policy (FK behavior, timestamp precision, symbol column collation) in one
helper does not protect it -- a new table can be written without using the
helper and silently drift. What is guarded here is the invariant itself, not
the helper.
"""

from __future__ import annotations

from sqlalchemy import Date, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base


def _symbol_fks() -> list[tuple[str, ForeignKeyConstraint]]:
    out = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_key_constraints:
            if fk.referred_table.name == "symbols":
                out.append((table.name, fk))
    return out


def test_every_symbol_fk_uses_the_same_policy() -> None:
    """ON UPDATE CASCADE + ON DELETE RESTRICT.

    RESTRICT enforces the soft-delete policy at the DB level: a single DELETE
    must not irreversibly wipe 40 years of history. If a table drifted to
    CASCADE, nobody would notice until a symbol got deleted and data vanished
    with it.
    """
    found = _symbol_fks()
    assert found, "no table with an FK to symbols found"
    for table_name, fk in found:
        assert fk.ondelete == "RESTRICT", f"{table_name}: ondelete={fk.ondelete}"
        assert fk.onupdate == "CASCADE", f"{table_name}: onupdate={fk.onupdate}"


def test_every_symbol_column_shares_the_symbols_collation() -> None:
    """An FK column's collation must exactly match its parent's.

    MySQL enforced this at the engine level: a mismatch raised ERROR 3780 and
    the table would not even get created. PostgreSQL raises no such error --
    the drift is silent and splits JOIN/comparison semantics between parent
    and child. That silence is exactly why this test matters."""
    parent = Base.metadata.tables["symbols"].c["symbol"].type
    for table_name, fk in _symbol_fks():
        for element in fk.elements:
            child = element.parent.type
            assert getattr(child, "collation", None) == getattr(parent, "collation", None), (
                f"{table_name}.{element.parent.name}"
            )
            assert getattr(child, "length", None) == getattr(parent, "length", None)


def test_no_timestamp_column_loses_sub_second_precision() -> None:
    """All timestamps are TIMESTAMP(6) WITH TIME ZONE.

    Second precision would collide within the same second on the
    (symbol, fetched_at) PK of ticker_info_history, and PostgreSQL rounds
    the fraction rather than truncating it.

    `timezone` is checked too: a naive timestamp column still produces a
    correct-looking result because psycopg interprets it against the
    connection's TZ, which papers over the type mismatch -- the drift is
    silent.
    """
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.c
        if isinstance(col.type, TIMESTAMP)
        and (col.type.precision != 6 or col.type.timezone is not True)
    ]
    assert offenders == []


# PK prefixes allowed to precede `as_of_date` in as-of tables. Scope is
# `symbol` on the symbol side, `domain_key` on the domain side; regional
# domain tables insert `region` in between, which does not break the
# invariant since the region set is config-bounded and known at query time --
# a "value as of day D" query still uses the prefix. `query_term` is a third
# scope axis: a search term's result carries multiple symbols, so its scope
# is not a symbol. A "result as of day D" query still uses the prefix:
# `WHERE query_term = ? AND as_of_date = ?`.
ASOF_PK_PREFIXES = (
    ("symbol",),
    ("domain_key",),
    ("domain_key", "region"),
    ("query_term",),
    # `screen_key`: a screen's daily headline and roster. `screen_quotes`
    # does NOT belong here -- its prefix is `symbol` because a quote is
    # independent of the screen.
    ("screen_key",),
)


def test_as_of_date_is_always_the_second_key_component() -> None:
    """If as_of_date is part of the key, it must be the second component.

    A "value as of day D" query relies on the `(scope[, region], as_of_date)`
    prefix; if as_of_date were last, that query would become a full scan.

    Gate tables (`asof_state`, `domain_asof_state`) are out of scope: there,
    as_of_date is not part of the key, it is data the gate carries.
    """
    checked = 0
    for table in Base.metadata.tables.values():
        pk = [c.name for c in table.primary_key.columns]
        if "as_of_date" not in pk:
            continue
        position = pk.index("as_of_date")
        assert tuple(pk[:position]) in ASOF_PK_PREFIXES, f"{table.name} -> {pk}"
        assert isinstance(table.c["as_of_date"].type, Date)
        checked += 1
    assert checked >= 14, f"expected as-of tables, found {checked}"


def test_child_tables_inherit_their_parent_timestamp() -> None:
    """Every timestamp-less table is either derived from a parent or source-dated.

    Not every row needs its own `fetched_at`: `financial_facts` takes its
    timestamp from `financial_periods`, `news_symbols` from `news`;
    `price_history`/`dividends`/`splits` carry the source's own date. This
    test documents that split so a new timestamp-less table must state which
    category it falls into.
    """
    source_dated = {
        "price_history",
        "dividends",
        "splits",
        "capital_gains",
        "shares_full",
        "company_officers",
        "news",
        # price_bars: ts_utc is the source's own time, like session_date on
        # price_history. A fetched_at would also cost 8 bytes x ~464M rows =
        # ~4 GB in the permanent archive for no question it answers -- which
        # run wrote a bar is already in sync_run_items.
        "price_bars",
        # periodic_bars: same rationale as price_bars.
        "periodic_bars",
    }
    # Operational/audit tables: they carry their own time columns
    # (added_at / detected_at / applied_at) but those are not "fetched from
    # source" timestamps, so fetched_at is not expected here.
    exempt = {
        "symbols",
        "sync_runs",
        "sync_run_items",
        "proxies",
        "alembic_version",
        "intraday_scope",  # added_at: when it entered the intraday scope list
        "bar_gaps",  # detected_at: when the gap was detected
        "bar_rescales",  # applied_at: when the rescale was applied
        # `screens` is static identity, same category as `symbols`: a
        # screen's definition is not fetched from Yahoo, it is seeded from
        # screens.py. It carries created_at/updated_at; fetched_at would be
        # the wrong meaning here. The daily fetch timestamp lives on
        # screen_runs.fetched_at.
        "screens",
        # `settings` is also static identity: written by the operator, not
        # fetched from Yahoo. Carries created_at/updated_at.
        "settings",
        # The stream tables carry an event time, not a fetch time. A tick
        # is not "fetched" at a moment we choose -- it arrives, and
        # `received_at` already records when. The rest are operational:
        # added_at (scope), created_at (outbox), started_at (sessions),
        # heartbeat_at (health), updated_at (offset, quotes).
        "live_ticks",
        "live_quotes",
        "stream_scope",
        "stream_outbox",
        "stream_relay_offset",
        "stream_rejects",
        "stream_sessions",
        "stream_connection_health",
    }
    for table in Base.metadata.tables.values():
        # The API's own tables (api_*) hold credentials, plans and usage
        # counters. Nothing in them is fetched from a source, so
        # "when was this fetched" has no meaning to answer; they carry
        # created_at / updated_at / day instead. Excluded by prefix rather
        # than listed one by one, since the set will keep growing.
        if table.name.startswith("api_"):
            continue
        if table.name in exempt or "fetched_at" in table.c:
            continue
        parents = {fk.referred_table.name for fk in table.foreign_key_constraints}
        assert table.name in source_dated or (parents - {"symbols"}), (
            f"{table.name}: has no fetched_at and derives from no parent"
        )


# Reserved/dangerous words that look tempting as column names.
#
# Built up in the MySQL era and kept through the move to PostgreSQL: most
# entries (window functions, `interval`, `order`, `group`, `key`, `rows`) are
# also reserved or type/function names in PostgreSQL. A few may be reserved
# only in MySQL, but the list is not trimmed since the point is portability:
# a name that works unquoted on both engines can never be broken by raw SQL.
# `status` is NOT on this list.
#
# `rank` was added because it is a reserved window function name and breaks
# any unquoted raw SQL with a syntax error. The ORM path works fine since
# SQLAlchemy quotes its own generated SQL -- the failure only surfaces in
# hand-written queries and migration scripts, i.e. as late as possible. This
# test exists to remove that delay.
RESERVED_WORDS = frozenset(
    {
        "rank",
        "range",
        "row_number",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lead",
        "lag",
        "first_value",
        "last_value",
        "groups",
        "over",
        "window",
        "recursive",
        "system",
        "of",
        "except",
        "interval",
        "key",
        "order",
        "group",
        "rows",
        "condition",
    }
)

# Pre-existing exception. `history_metadata.range` is the source's own field
# name (`range: "1mo"`) and the table is in production. Renaming it would
# mean a migration plus a data move, and this test exists to stop NEW tables
# from falling into the same trap, not to rewrite history. Before adding to
# this list: make sure the name is truly unavoidable.
RESERVED_GRANDFATHERED = frozenset({"history_metadata.range"})


def test_no_column_uses_a_reserved_word() -> None:
    """Column names are never chosen from reserved words.

    `economic_calendar.last_reported` was the first case this rule caught;
    `screen_members.rank_index` the second. Both stand in for a "natural"
    name (`reported`, `rank`) that turned out to be reserved.
    """
    offenders = [
        name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.lower() in RESERVED_WORDS
        and (name := f"{table.name}.{column.name}") not in RESERVED_GRANDFATHERED
    ]
    assert offenders == [], offenders


# --- what the migrations actually create ------------------------------------
#
# `alembic check` cannot see any of this. Autogenerate does not compare CHECK
# constraints at all, and it cannot read an expression index back from the
# database to compare it either -- so both drifted silently for the whole
# life of the schema, and the test suite hid the drift rather than exposing
# it: conftest builds its schema with `create_all`, which emits everything
# the models declare. Every constraint was enforced in tests and absent from
# a migrated database.


def _migration_text() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    return "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))


def _declared_checks() -> dict[str, str]:
    from sqlalchemy import CheckConstraint

    import yfin.api.models  # noqa: F401 - registers the API tables

    found: dict[str, str] = {}
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                found[constraint.name] = table.name
        # Column-level checks do not appear in `table.constraints`, which is
        # how thirty-nine of them stayed out of sight: a scan that only
        # walked table constraints reported eight and looked complete.
        for column in table.columns:
            for constraint in column.constraints or ():
                if isinstance(constraint, CheckConstraint) and constraint.name:
                    found[constraint.name] = table.name
    return found


def test_every_declared_CHECK_is_created_by_a_migration() -> None:
    text = _migration_text()
    missing = sorted(
        f"{table}.{name}" for name, table in _declared_checks().items() if name not in text
    )
    assert missing == [], (
        "these CHECK constraints exist in the models and in every test database, "
        f"and in no migrated one: {missing}"
    )


def test_every_declared_INDEX_is_created_by_a_migration() -> None:
    import yfin.api.models  # noqa: F401

    text = _migration_text()
    missing = sorted(
        f"{table.name}.{index.name}"
        for table in Base.metadata.sorted_tables
        for index in table.indexes
        if index.name and index.name not in text
    )
    assert missing == [], f"declared but never created: {missing}"
