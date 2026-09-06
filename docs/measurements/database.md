# PostgreSQL / TimescaleDB — measured behaviour

Measured against the pinned image: **PostgreSQL 18.6 + TimescaleDB
2.29.2** (`timescale/timescaledb:2.29.2-pg18`).

Each entry exists because the schema or the write path depends on it.

## Types

| Claim | Observation |
|---|---|
| Generic `sqlalchemy.TIMESTAMP` rejects `precision` | `TypeError: TIMESTAMP.__init__() got an unexpected keyword argument 'precision'`. The dialect type is required. |
| `timestamptz(n)` **rounds**, does not truncate | `.123456789` → `.123457`; `timestamptz(0)` given `.9` → +1 second. Hence 6 digits everywhere: second precision would collide on `(symbol, fetched_at)` keys. |
| `NUMERIC(p,s)` rounds the fraction silently | `numeric(5,2)` given `1.239` stores `1.24`, no warning. Rounding is therefore done explicitly in Python. |
| `NUMERIC` overflow is loud | `numeric(5,2)` given `12345.6` → `22003 numeric field overflow`. Over-long varchar → `22001`. |
| No unsigned integers | Replaced by `CHECK (col >= 0)` / range checks. |
| Row size | No 65,535-byte limit; wide values move to TOAST. The practical limit is 1,600 columns. |
| btree index tuple limit | 2,704 bytes. `repeat('x',2704)` indexes; 2,705 → *"index row size 2720 exceeds btree version 4 maximum 2704"*. This is the tuple size including headers, not the payload. |

## Collation

`COLLATE "C"` is byte-ordered and case-sensitive, and it keeps `LIKE`
index-usable **without** `text_pattern_ops`:

```
"C" column:          Index Scan using ix_c ... Index Cond: (s >= 'abc' AND s < 'abd')
en_US.utf8 column:   Seq Scan
```

Every string column uses it. Case-insensitive behaviour the domain needs
is applied on the write path instead.

## Upsert

| Claim | Observation |
|---|---|
| `ON CONFLICT` target must match a unique constraint **exactly, as a set** | PK `(a,b)`: `on conflict (b,a)` works; `(a)` and `(a,b,c)` both raise *"there is no unique or exclusion constraint matching the ON CONFLICT specification"*. |
| Partial unique index needs a predicate for `DO UPDATE` | `on conflict (c) do nothing` works; `do update` raises unless `where v='q'` is repeated. |
| `DO UPDATE` cannot touch a row twice in one statement | `21000 cardinality_violation` — *"ON CONFLICT DO UPDATE command cannot affect row a second time"*. Batches are deduplicated before insert. |
| `DO NOTHING` does not count skipped rows | `INSERT 0 0`. Row counts are unusable for verification, so writes are verified by a separate key-existence read. |
| `GREATEST` ignores NULL | `greatest(true, NULL::boolean)` → `true`; `greatest(5, NULL::int)` → `5`. |
| `GREATEST` works on booleans | `greatest(false, true)` → `true`. Monotonic columns rely on both of these. |

## Advisory locks

Advisory locks are **database-scoped**, and the key is a signed 64-bit
integer split across two `oid` columns in `pg_locks`.

```sql
SELECT pg_try_advisory_lock(123456789012345::bigint);   -- t
SELECT classid, objid, objsubid FROM pg_locks WHERE locktype='advisory';
 classid |   objid    | objsubid
   28744 | 2249056121 |        1
```

- `objsubid` is **1** for the single-`bigint` form and **2** for the
  two-`int` form. Querying with the wrong one silently returns nothing.
- `classid`/`objid` are `oid` (unsigned 32-bit). `(:key >> 32)::int`
  raises `22003` once the low half exceeds 2³¹, and `>>` on a negative
  key sign-extends rather than yielding the high half. The split is
  therefore computed in Python, not in SQL.
- For the project's own `yfin_sync` key both failure conditions hold:
  the key is `-5176397690111661269` and `classid` is `3089743290`.

## ENUM types

| Claim | Observation |
|---|---|
| Values are stored as `pg_enum` OIDs, not ordinals | `ALTER TYPE ek ADD VALUE 'x' BEFORE 'y'` set `enumsortorder` to 1.5 and left a row with a composite FK intact. Insertion order is safe. |
| A new value cannot be used in the transaction that adds it | *"unsafe use of new value ... must be committed before they can be used"*. |
| SQLAlchemy deduplicates same-named `Enum` objects in one `MetaData` | Two separate objects with the same name emit one `CREATE TYPE`, even with `checkfirst=False`. |
| Unnamed `Enum` built from a Python enum class still gets a name | Derived from the class (`proxyscheme`). Only a bare list of strings raises *"PostgreSQL Enum type requires a name"*. |
| `op.drop_table` does **not** drop the type | The downgrade succeeds and the *next* upgrade fails with *"type ... already exists"*. Types are dropped explicitly. |

## TimescaleDB

| Claim | Observation |
|---|---|
| `by_range(col, INTERVAL)` accepts a `DATE` column | `price_history` partitions on `session_date`. |
| `INTERVAL '1 year'` becomes 360 days | Months are treated as 30 days. `INTERVAL '365 days'` is used where the exact value matters. |
| Every unique index must contain the partitioning column | Otherwise: *"cannot create a unique index without the column "ts_utc" (used in partitioning)"*. |
| `create_hypertable` adds a default DESC index on the partitioning column | It lives in `public`, is absent from the model metadata, and autogenerate reports it as a deletion — so `create_default_indexes => FALSE` is mandatory. |
| A hypertable can be the referencing side of an FK | `ON UPDATE CASCADE` and `ON DELETE RESTRICT` both work, and `drop_chunks` is unaffected by an outgoing FK. |
| `DROP SCHEMA ... CASCADE` removes chunks too | *"drop cascades to table _timescaledb_internal._hyper_1_1_chunk"*; no chunk leak. |
| Chunk exclusion works through a plain view | `Custom Scan (ChunkAppend)` with the index condition pushed to chunk level. |
| `timescaledb_information.*` is not schema-filtered | The same hypertable name in two schemas returns two rows; queries must filter on `hypertable_schema`. |
| The extension installs into `public` | `create_hypertable` fails with *"function by_range(unknown, interval) does not exist"* unless `public` is on `search_path`. |

## Bootstrap

| Claim | Observation |
|---|---|
| `CREATE DATABASE` cannot run in a transaction | *"CREATE DATABASE cannot run inside a transaction block"* — the connection must be AUTOCOMMIT. |
| There is no `CREATE DATABASE IF NOT EXISTS` | Existence is checked against `pg_database`. |
| `CREATE EXTENSION` is per-database | Run once per database. In this image the extension is preinstalled into `template1`, so the call is idempotent. |
| `information_schema` is database-scoped | A connection to the `postgres` maintenance database cannot see schemas inside another database. Schema cleanup must connect to the target database. |
| `now()` takes no arguments | `now(6)` → *"function now(integer) does not exist"*. Precision comes from the column type. |
