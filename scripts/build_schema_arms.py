#!/usr/bin/env python
"""Build the two schemas whose difference the parity gate pins.

WHY THIS EXISTS
---------------
``scripts/apply_migrations.py`` BASELINES: on the first run against a schema that
already has the app tables it stamps every ``.sql`` file as applied WITHOUT
running it. ``main.py:_run_db_bootstrap`` calls ``create_tables()`` (SQLAlchemy
``create_all``) immediately BEFORE it, and that creates the table the probe looks
for — so the probe reads true on a brand-new empty database too, and **every
migration is baselined away on a fresh database, permanently**.

That is correct behaviour (see ``apply_migrations``' docstring for why replaying
the corpus over a ``create_all``'d schema is unsafe), but it means the ORM is the
*only* thing that builds a fresh database. Anything a migration expresses that
``models.py`` does not is silently absent there, forever. PR #128 found three
such divergences by hand. This script measures the whole set instead.

THE TWO ARMS
------------
``orm``     ``Base.metadata.create_all()`` into an empty database.
            What a FRESH database actually gets.

``corpus``  ``init.sql`` -> the one-statement genesis shim -> ``001..021`` forced,
            unguarded by the baseline. What the migration corpus SAYS the schema
            is.

THE CORPUS ARM IS BUILT FROM EMPTY, NEVER ON TOP OF THE ORM ARM.
This is the single most important decision here, and the obvious alternative is
wrong in a way that is silent: 15 of the 21 migrations are guarded
(``ADD COLUMN IF NOT EXISTS``, ``CREATE TABLE IF NOT EXISTS``,
``CREATE INDEX IF NOT EXISTS``, ``DO $$ ... IF NOT EXISTS (SELECT 1 FROM
pg_constraint ...)``). Replayed OVER a ``create_all``'d schema they all
short-circuit, so "the corpus adds nothing" comes out true and VACUOUS — and
migration 001's ``DEFAULT 60`` on ``plan.rate_limit_rpm``, which prod has and a
fresh database does not, stays invisible. A gate built that way passes today
while the divergence it exists to catch sits in the schema.

EACH ARM GETS ITS OWN THROWAWAY DATABASE, not merely its own schema — and that
is forced by a real defect in the corpus, not by preference. See
``_recreate_database`` for the full account: migration 016 probes
``pg_constraint`` with no namespace filter, so any other copy of the app schema
anywhere in the same database (the other arm, or the ``public`` schema the pytest
conftest builds) makes its guard skip the ADD and the following VALIDATE fail.

This needs CREATE DATABASE on the target server. The CI archetype's postgres
service and a local ``postgres:18`` container both connect as a superuser, so
that holds; if it ever does not, the failure is loud rather than silent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_migrations import MIGRATIONS_DIR, split_statements  # noqa: E402
from scripts.dump_schema import dump_schema  # noqa: E402

INIT_SQL = ROOT / "init.sql"
SHIM_SQL = ROOT / "tests" / "support" / "schema_genesis_shim.sql"

SNAPSHOT_DIR = ROOT / "docs" / "schema"
ORM_SNAPSHOT = SNAPSHOT_DIR / "orm.json"
CORPUS_SNAPSHOT = SNAPSHOT_DIR / "migrations.json"


class ArmBuildError(RuntimeError):
    """A schema arm could not be built. NEVER swallowed — a corpus arm that
    silently skipped a failing migration yields a SMALLER divergence set, which
    reads as an improvement. That is the failure mode this exception exists to
    make impossible."""


def _admin_dsn(dsn: str) -> str:
    """The same server, connected to the always-present ``postgres`` database."""
    head, _, _tail = dsn.rpartition("/")
    return f"{head}/postgres"


def _dsn_for_db(dsn: str, dbname: str) -> str:
    head, _, _tail = dsn.rpartition("/")
    return f"{head}/{dbname}"


def _recreate_database(dsn: str, dbname: str) -> str:
    """Drop and recreate a throwaway database, returning its DSN.

    ⚠️ EACH ARM NEEDS ITS OWN DATABASE — schemas are not enough, and the reason
    is a real defect in the corpus rather than a quirk of this harness.
    Migration 016's guard is:

        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_license_tenant')

    ``pg_constraint`` is queried with **no namespace filter**, so it matches that
    constraint name in ANY schema of the database. Build the corpus arm in a
    database that already holds the app schema anywhere — the ORM arm's schema,
    or the ``public`` schema the pytest conftest builds — and the guard sees the
    other copy, skips the ADD, and the next statement
    (``ALTER TABLE license VALIDATE CONSTRAINT fk_license_tenant``) fails with
    "constraint ... does not exist".

    Harmless in production, where licenses owns a single ``public`` schema in a
    dedicated database. But it makes 016 non-idempotent under any multi-schema
    layout, and it is precisely the sort of thing this gate exists to surface.
    Recorded here rather than silently worked around.
    """
    with psycopg.connect(_admin_dsn(dsn), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return _dsn_for_db(dsn, dbname)


def _drop_database(dsn: str, dbname: str) -> None:
    try:
        with psycopg.connect(_admin_dsn(dsn), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
    except Exception:  # noqa: BLE001 - best-effort cleanup, never masks a real failure
        pass


def _run_sql_file(conn: psycopg.Connection, path: Path, label: str) -> None:
    """Execute one .sql file with the PRODUCTION splitter.

    Reusing ``split_statements`` rather than reimplementing it means 008's
    ``CREATE INDEX CONCURRENTLY`` and 005/016's embedded ``BEGIN;/COMMIT;``
    behave exactly as they do in the real runner, and the splitter is itself
    exercised by this gate.
    """
    if not path.exists():
        raise ArmBuildError(f"{label}: missing file {path}")
    for stmt in split_statements(path.read_text(encoding="utf-8")):
        try:
            with conn.cursor() as cur:
                # Encoded rather than passed as `str`: psycopg types `execute` to
                # take a LiteralString (an injection guard), and SQL read from a
                # file can never be one. `bytes` is an equally first-class
                # overload and executes identically, so this states "trusted
                # repo-controlled SQL" without suppressing the checker.
                cur.execute(stmt.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - re-raised with provenance
            raise ArmBuildError(
                f"{label}: {path.name} failed: {exc}\n--- statement ---\n{stmt[:400]}"
            ) from exc


def build_orm_arm(dsn: str, dbname: str) -> dict:
    """Arm A: what a fresh database gets."""
    from sqlalchemy import create_engine

    import models

    arm_dsn = _recreate_database(dsn, dbname)
    try:
        # psycopg v3 explicitly: this fleet has no psycopg2, and SQLAlchemy's
        # default `postgresql://` dialect resolves to psycopg2.
        sa_dsn = arm_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        engine = create_engine(sa_dsn)
        try:
            models.Base.metadata.create_all(bind=engine)
        finally:
            engine.dispose()

        with psycopg.connect(arm_dsn, autocommit=True) as conn:
            return dump_schema(conn, "public")
    finally:
        _drop_database(dsn, dbname)


def build_corpus_arm(dsn: str, dbname: str) -> dict:
    """Arm B: what the migration corpus says the schema is. FROM EMPTY."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise ArmBuildError(f"no .sql files in {MIGRATIONS_DIR}")

    arm_dsn = _recreate_database(dsn, dbname)
    try:
        with psycopg.connect(arm_dsn, autocommit=True) as conn:
            _run_sql_file(conn, INIT_SQL, "genesis")
            _run_sql_file(conn, SHIM_SQL, "shim")
            for path in files:
                _run_sql_file(conn, path, "corpus")

            snapshot = dump_schema(conn, "public")
    finally:
        _drop_database(dsn, dbname)

    snapshot["_provenance"] = {
        "genesis": INIT_SQL.name,
        "shim": SHIM_SQL.name,
        "migration_files": [p.name for p in files],
    }
    return snapshot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--mode", choices=("orm", "corpus", "both"), default="both")
    ap.add_argument(
        "--dbname", default=None, help="throwaway database name (default: parity_build_<mode>)"
    )
    ap.add_argument("--out", default=None, help="write JSON here instead of stdout")
    ap.add_argument("--refresh-snapshots", action="store_true", help="write docs/schema/*.json")
    args = ap.parse_args(argv)

    builders = {"orm": build_orm_arm, "corpus": build_corpus_arm}
    modes = ["orm", "corpus"] if args.mode == "both" else [args.mode]

    results: dict[str, dict] = {}
    for mode in modes:
        dbname = args.dbname or f"parity_build_{mode}"
        results[mode] = builders[mode](args.dsn, dbname)

    if args.refresh_snapshots:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for mode, path in (("orm", ORM_SNAPSHOT), ("corpus", CORPUS_SNAPSHOT)):
            if mode in results:
                path.write_text(
                    json.dumps(results[mode], indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                print(f"[build_schema_arms] wrote {path.relative_to(ROOT)}", flush=True)
        return 0

    payload = results if len(results) > 1 else next(iter(results.values()))
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
