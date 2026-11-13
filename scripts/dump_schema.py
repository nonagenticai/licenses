#!/usr/bin/env python
"""Dump a postgres schema's structure as normalized, comparable JSON.

Used by ``scripts/build_schema_arms.py`` to snapshot both parity arms, and by
``tests/unit/test_schema_parity.py`` to compare them.

NORMALIZATION — every rule, and why. Each entry here is a difference the gate can
no longer see, so this list is deliberately short and each entry is justified:

* **extension-owned relations** (``pg_depend.deptype = 'e'``) — prod carries
  ``pg_stat_statements``/``pg_stat_statements_info`` from a DBA
  ``CREATE EXTENSION``. They are on neither arm and belong to no migration.
  Filtering by ``pg_depend`` rather than by a name blocklist means a future
  extension is handled without a code change.
* **``schema_migrations``** — the runner's own bookkeeping table, created by
  ``apply_all``. It is in no migration and no model.
* **column ordinal position** — columns are keyed by NAME, not ``attnum``.
  ``create_all`` emits class-declaration order; ``ALTER TABLE ... ADD COLUMN``
  appends. This divergence is unavoidable and unfixable, so comparing it would
  be permanent noise.
* **whitespace runs** inside ``pg_get_*def`` output — postgres already
  canonicalizes these expressions; only formatting varies.
* **``contype = 'n'``** — PG18 materializes NOT NULL as ``<table>_<col>_not_null``
  constraint rows. Nullability is already compared per column; including these
  double-reports every nullability difference under an auto-generated name.

EXPLICITLY NOT NORMALIZED, because each is a real divergence someone will be
tempted to hide:

* ``text`` vs ``character varying(n)`` — different length enforcement.
* server defaults (``gen_random_uuid()``, ``now()``, ``60``, or absent). A raw
  INSERT that works on one schema fails on the other.
* constraint NAMES — ``fk_license_tenant`` vs ``license_tenant_id_fkey`` is the
  exact divergence PR #128 found by hand. A name-insensitive comparator would
  have missed it.
* partial-index WHERE predicates and expression-index bodies — ``COALESCE``
  inside a unique index changes NULL semantics.
* ``convalidated`` — a NOT VALID FK enforces new rows only.
* collation, identity and generated columns — empty today, captured so a future
  one is visible rather than silently accepted.

DATA IS OUT OF SCOPE. The corpus arm ends with rows (003's random-uuid7 tenants,
013/014's seeds) the ORM arm does not have; comparing data would drown the schema
signal in irreproducible uuids. Data parity for the hardcoded platform identities
is owned by ``tests/unit/test_bootstrap_seed.py::TestPlatformTenantsAreSeeded``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import LiteralString

import psycopg

# The runner's own bookkeeping. Not part of either arm's declared schema.
RUNNER_TABLE = "schema_migrations"

_WS = re.compile(r"\s+")

# Inlined into each query below rather than f-string-composed: psycopg types
# `Cursor.execute` to accept only a LiteralString (a real injection guard), and
# an f-string is not one. Duplication here is the price of keeping every query
# statically trusted without a `type: ignore`.
Q_TABLES = """
SELECT c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind = 'r' AND NOT EXISTS (SELECT 1 FROM pg_depend d
                 WHERE d.objid = c.oid AND d.deptype = 'e')
 ORDER BY 1
"""

Q_COLUMNS = """
SELECT c.relname, a.attname,
       format_type(a.atttypid, a.atttypmod)   AS type,
       a.attnotnull                           AS not_null,
       pg_get_expr(ad.adbin, ad.adrelid)      AS default_expr,
       a.attidentity, a.attgenerated,
       CASE WHEN a.attcollation <> 0
            THEN (SELECT collname FROM pg_collation WHERE oid = a.attcollation)
       END                                    AS collation
  FROM pg_attribute a
  JOIN pg_class c     ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
 WHERE n.nspname = %s AND c.relkind = 'r'
   AND a.attnum > 0 AND NOT a.attisdropped AND NOT EXISTS (SELECT 1 FROM pg_depend d
                 WHERE d.objid = c.oid AND d.deptype = 'e')
 ORDER BY 1, 2
"""

Q_INDEXES = """
SELECT c.relname AS table_name, i.relname AS index_name,
       x.indisunique, x.indisprimary,
       pg_get_indexdef(x.indexrelid) AS def
  FROM pg_index x
  JOIN pg_class i     ON i.oid = x.indexrelid
  JOIN pg_class c     ON c.oid = x.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind = 'r' AND NOT EXISTS (SELECT 1 FROM pg_depend d
                 WHERE d.objid = c.oid AND d.deptype = 'e')
 ORDER BY 1, 2
"""

# contype 'n' (NOT NULL, PG18) is excluded on purpose — see module docstring.
Q_CONSTRAINTS = """
SELECT c.relname, con.conname, con.contype, con.convalidated,
       pg_get_constraintdef(con.oid) AS def
  FROM pg_constraint con
  JOIN pg_class c     ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind = 'r'
   AND con.contype IN ('p','u','f','c') AND NOT EXISTS (SELECT 1 FROM pg_depend d
                 WHERE d.objid = c.oid AND d.deptype = 'e')
 ORDER BY 1, 2
"""

Q_SEQUENCES = """
SELECT c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind = 'S' AND NOT EXISTS (SELECT 1 FROM pg_depend d
                 WHERE d.objid = c.oid AND d.deptype = 'e')
 ORDER BY 1
"""

Q_EXTENSIONS = "SELECT extname FROM pg_extension ORDER BY 1"


def _canon(value):
    """Collapse whitespace runs in catalog-rendered SQL. Nothing else."""
    if isinstance(value, str):
        return _WS.sub(" ", value).strip()
    return value


def _strip_schema(text: str, schema: str) -> str:
    """Remove the throwaway schema name so two arms built in differently-named
    schemas still compare equal. Without this every index/constraint def would
    differ on the schema qualifier alone and the gate would be permanently red.
    """
    return text.replace(f'"{schema}".', "").replace(f"{schema}.", "")


def dump_schema(conn: psycopg.Connection, schema: str) -> dict:
    def rows(query: LiteralString) -> list[tuple]:
        with conn.cursor() as cur:
            cur.execute(query, (schema,))
            return cur.fetchall()

    tables = [r[0] for r in rows(Q_TABLES) if r[0] != RUNNER_TABLE]

    columns: dict[str, dict] = {}
    for tbl, col, typ, not_null, default, ident, gen, coll in rows(Q_COLUMNS):
        if tbl == RUNNER_TABLE:
            continue
        columns[f"{tbl}.{col}"] = {
            "type": typ,
            "not_null": bool(not_null),
            "default": _strip_schema(_canon(default), schema) if default is not None else None,
            "identity": ident or None,
            "generated": gen or None,
            "collation": coll,
        }

    indexes: dict[str, dict] = {}
    for tbl, name, uniq, primary, definition in rows(Q_INDEXES):
        if tbl == RUNNER_TABLE:
            continue
        indexes[f"{tbl}.{name}"] = {
            "unique": bool(uniq),
            "primary": bool(primary),
            "def": _strip_schema(_canon(definition), schema),
        }

    constraints: dict[str, dict] = {}
    for tbl, name, contype, validated, definition in rows(Q_CONSTRAINTS):
        if tbl == RUNNER_TABLE:
            continue
        constraints[f"{tbl}.{name}"] = {
            "type": contype,
            "validated": bool(validated),
            "def": _strip_schema(_canon(definition), schema),
        }

    sequences = [r[0] for r in rows(Q_SEQUENCES)]

    with conn.cursor() as cur:
        cur.execute(Q_EXTENSIONS)
        extensions = [r[0] for r in cur.fetchall()]

    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "sequences": sequences,
        "extensions": extensions,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dump a schema as normalized JSON")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--schema", default="public")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        snapshot = dump_schema(conn, args.schema)

    text = json.dumps(snapshot, indent=2, sort_keys=True)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
