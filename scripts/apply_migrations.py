#!/usr/bin/env python3
"""Apply migrations/*.sql on startup, tracked so each file runs at most once.

licenses has no migration framework — startup only runs
``Base.metadata.create_all()``, which creates missing TABLES from the ORM but
never ALTERs an existing table to add a column. So every column-add to an
existing table (e.g. offering.access_price_usd in 011) had to be hand-applied to
prod before the pod rolled, or every ``query(Offering)`` 500'd. This runner
closes that trap so flux/code is the source of truth, not a hand-run psql.

Design — tracking table + baseline (run each file AT MOST ONCE):
  A ``schema_migrations(filename, applied_at)`` table records what has run.
  Re-running the whole set every startup is NOT safe: several files are only
  idempotent in isolation, not in sequence against later data — e.g. 006 adds
  the NARROW ck_tenants_byo_mode CHECK, and re-running it after 007 introduced
  ``platform_only`` rows violates the constraint. So each file runs once.

  BASELINE: on the FIRST run against an already-populated schema (detected by
  the ``offering`` table existing — created by 010, and by create_all on a fresh
  DB), all current files are STAMPED as applied WITHOUT running them. On prod
  they are already hand-applied; on a fresh DB create_all already built the full
  current schema and there is no data for the backfills to touch. From then on,
  only NEW (unstamped) files run — the actual point of the runner.

  ⛔ THE PRICE OF THAT, NAMED. This runner NEVER replays the corpus on a fresh
  database, so ``models.py`` is the ONLY thing that builds one. Anything a
  migration expresses that the ORM does not is silently absent there, forever —
  measured 2026-08-12 at 46 divergences. (#128 found three instances of the same
  CLASS by hand and fixed them; its two schema findings are consequently in BOTH
  arms today and are NOT among the 46, and its third was missing seed DATA, which
  a schema gate cannot count. The 46 is the set that remained after it.) The ONLY
  thing standing between that and the next #128 is
  ``tests/unit/test_schema_parity.py``. Delete it, mute it, or bump its pin
  without reading the diff, and the defect class re-opens in full.

  This behaviour is deliberate, not an oversight, and flipping the probe does not
  fix it — it buys crashes without buying parity. Verified individually, rather
  than asserted as a count of "guarded files", because guardedness and no-opping
  are not the same property and the tempting summary gets it wrong:

    * most files carry ``IF NOT EXISTS`` / ``pg_constraint`` guards and simply
      no-op against a create_all'd schema, so the divergences stay invisible —
      001's ``DEFAULT 60`` on ``plan.rate_limit_rpm`` is the concrete example;
    * 004 and 016 do not merely "run against the wrong schema", they hard-ERROR:
      004's ``WHERE l.tenant_id = t.name`` and 016's ``t.id::text = l.tenant_id``
      compare ``uuid`` to ``varchar``, which Postgres rejects at PLAN time since
      ``License.tenant_id`` became ``PGUUID`` in #128 — so an empty table does not
      save them and the pod refuses to serve;
    * 006 is guarded on the COLUMN but its ``ADD CONSTRAINT`` is not: it drops
      ``ck_tenants_byo_mode`` and re-adds it with TWO values, while ``models.py``
      declares THREE. Replayed, it narrows the ORM's own constraint and fails
      outright against any ``platform_only`` row;
    * 014's ``ON CONFLICT (id) DO UPDATE`` would re-affirm a byo_mode posture an
      operator may have deliberately changed.

  What WAS wrong is that none of it was said out loud:
  ``main.py:_run_db_bootstrap`` now asks ``db_manager.database_is_pristine()``
  BEFORE ``create_tables()`` and WARNs when a database is genuinely empty. That
  adds one bounded extra connect per boot (``connect_timeout``-governed) and
  changes NOTHING about the baseline decision below.

Execution:
  Each file is split into top-level statements (respecting dollar-quoted ``$$``
  blocks, string literals, and comments) and each statement runs on an
  autocommit connection, so files that carry their own ``BEGIN;``/``COMMIT;``
  (005) still group correctly.

  ⛔ ``CREATE INDEX CONCURRENTLY`` is NO LONGER RUNNABLE HERE. It used to be the
  reason for the autocommit connection, and 008 used it — but 008 is stamped
  everywhere and the serialisation added below (see Concurrency) holds an open
  transaction for the whole run, which CIC waits on for ever. A PENDING file
  containing one is refused with a named error instead of hanging the boot; the
  trade-off and the way out are written down in
  ``_reject_out_of_transaction_statements``.

Concurrency (``replicas: 2``):
  The whole pending run — tracking DDL, the ``schema_migrations`` read, and every
  statement of every pending file — is serialised behind one Postgres advisory
  lock (``migration_lock``). Without it the per-file ``IF NOT EXISTS`` /
  ``pg_constraint`` guards are only no-ops SERIALLY: two replicas booting together
  both read the guard as false and the second ``ADD CONSTRAINT`` dies ``42710``,
  which this runner turns into a non-zero exit and the lifespan turns into
  CrashLoopBackOff. The loser of the lock WAITS and then finds nothing pending.

Env:
  DATABASE_URL — SQLAlchemy-style URL (``postgresql+psycopg://…``); the
  ``+psycopg`` driver tag is stripped for the raw psycopg connection.
  MIGRATION_LOCK_TIMEOUT_MS — how long a migration statement may queue behind
  another backend's table lock before it is aborted (default 5000; 0 disables).
  Bounds the window in which a DDL's ACCESS EXCLUSIVE lock stalls the auth hot
  path, and turns a stuck DDL into a nameable boot error instead of a hang.

Exit codes: 0 = up to date (applied/baselined); 1 = a migration failed.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Start of a dollar-quote tag: $$ or $tag$ (tag = letters/digits/underscore).
_DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into top-level statements.

    Semicolons inside dollar-quoted blocks ($$…$$ / $tag$…$tag$), single-quoted
    strings, ``--`` line comments and ``/* */`` block comments do NOT terminate a
    statement. Comment/whitespace-only fragments are dropped.
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    dollar_tag: str | None = None
    in_squote = False
    while i < n:
        ch = sql[i]
        pair = sql[i : i + 2]
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if pair == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
            continue
        if pair == "/*":
            j = sql.find("*/", i)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _has_sql(stmt):
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if _has_sql(tail):
        stmts.append(tail)
    return stmts


def _has_sql(stmt: str) -> bool:
    """True if the fragment contains a statement, not just comments/whitespace."""
    without_line = re.sub(r"--[^\n]*", "", stmt)
    without_block = re.sub(r"/\*.*?\*/", "", without_line, flags=re.DOTALL)
    return bool(without_block.strip())


#: Identity of the platform-wide migration lock. A NAME rather than a bare int so
#: the derivation is auditable and collision-resistant against any other advisory
#: lock this database may carry; the key itself must be a stable 64-bit integer
#: because that is what ``pg_advisory_xact_lock`` takes.
MIGRATION_LOCK_NAME = "nonagenticai.licenses.apply_migrations"

#: The 64-bit key. Derived deterministically from ``MIGRATION_LOCK_NAME`` so every
#: replica of every version of this file computes the SAME value without anyone
#: having to keep a magic number in sync. ``signed=True`` because Postgres
#: advisory-lock keys are ``bigint``.
MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(MIGRATION_LOCK_NAME.encode("utf-8"), digest_size=8).digest(),
    "big",
    signed=True,
)

#: Milliseconds a migration statement may WAIT for a table lock before it is
#: aborted. Not a statement timeout: this bounds only the time spent queueing
#: behind another backend's lock, which is the failure mode that matters here.
#: A migration taking ACCESS EXCLUSIVE on ``token`` or ``tenants`` queues behind
#: every in-flight reader AND blocks every reader that arrives after it — so an
#: unbounded wait does not merely stall the boot, it stalls ``introspect_token``
#: / ``_lookup_byo_mode`` on the platform-wide auth hot path for as long as the
#: wait lasts. Failing fast turns that into a nameable, retried boot error.
#: Overridable for the rare operator with a genuinely slow DDL; 0 disables it.
LOCK_TIMEOUT_ENV = "MIGRATION_LOCK_TIMEOUT_MS"
DEFAULT_LOCK_TIMEOUT_MS = 5000

#: Statements that cannot run inside a transaction block.
#:
#: They are refused outright when a PENDING file contains one (see
#: ``_reject_out_of_transaction_statements``), and — belt for the case where one
#: reaches execution anyway — they are never given the ``SET lock_timeout``
#: prefix, because libpq wraps a multi-statement simple query in an implicit
#: transaction, which is precisely what they refuse to run in.
_MUST_RUN_OUTSIDE_TRANSACTION = re.compile(r"\bCONCURRENTLY\b|\bVACUUM\b", re.IGNORECASE)


def lock_timeout_ms() -> int:
    """Configured lock_timeout in ms; 0 (or a non-integer) means "do not set one"."""
    try:
        value = int(os.getenv(LOCK_TIMEOUT_ENV, str(DEFAULT_LOCK_TIMEOUT_MS)))
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT_MS
    return max(value, 0)


def _with_lock_timeout(stmt: str, timeout_ms: int) -> str:
    """Prefix one migration statement with its own ``SET lock_timeout``.

    WHY PER STATEMENT AND NOT ONCE PER SESSION. Production reaches this database
    through **pgbouncer in transaction pooling mode** (``DATABASE_URL`` is
    ``pgbouncer.pgbouncer.svc.cluster.local:6432``). There, a server connection is
    assigned per TRANSACTION, and this runner is autocommit — so every statement
    is its own transaction and may land on a different backend. A session-level
    ``SET`` would therefore govern an arbitrary subset of the run and, worse, leak
    the setting to whichever unrelated client got that server connection next.
    Sent in the same simple query as the statement it guards, the ``SET`` is
    scoped to the same implicit transaction as the DDL, on the same backend, and
    is gone when that transaction ends.
    """
    if timeout_ms <= 0 or _MUST_RUN_OUTSIDE_TRANSACTION.search(stmt):
        return stmt
    return f"SET lock_timeout = {timeout_ms};\n{stmt}"


@contextmanager
def migration_lock(dsn: str) -> Iterator[None]:
    """Hold the platform-wide migration lock for the whole pending-migration run.

    THE DEFECT THIS CLOSES. Every guard in ``migrations/*.sql`` is a check-then-act
    (``IF NOT EXISTS (SELECT 1 FROM pg_constraint ...)`` / ``IF NOT EXISTS``), and
    a check-then-act is only a no-op SERIALLY. ``licenses`` runs ``replicas: 2``,
    and both replicas run this on startup: they compute the same pending list, both
    see the guard's condition as false, and the second ``ADD CONSTRAINT`` errors
    ``42710 duplicate_object``. That is not a cosmetic log line — ``_run_file``
    raises, ``apply_all`` returns 1, ``main._run_db_bootstrap`` raises
    ``MigrationError``, ``is_db_unreachable`` is False, the lifespan re-raises and
    uvicorn exits: **CrashLoopBackOff on one replica of the ext_authz service**.
    Reachable on ``kubectl delete pod --all``, first Deployment creation,
    scale-from-zero, a DR rebuild, and any pgbouncer blip that puts both pods into
    the ``DB_BOOTSTRAP_RETRY_SECONDS`` loop in lockstep.

    THE LOSER WAITS, IT DOES NOT FAIL. ``pg_advisory_xact_lock`` blocks rather than
    returning false, and the pending list is read INSIDE the lock — so the second
    replica wakes up, finds every file already stamped, and returns 0. Any
    ``pg_try_advisory_lock`` variant would have to invent a policy for "someone
    else has it", and the only correct policy is to wait.

    A TRANSACTION-SCOPED LOCK, ON A DEDICATED CONNECTION, ON PURPOSE. Two reasons,
    both about pgbouncer transaction pooling (see ``_with_lock_timeout``):

    * a SESSION-level ``pg_advisory_lock`` would be taken on whichever server
      connection served that one statement and would NOT be held for the
      autocommit statements that follow — and the matching ``pg_advisory_unlock``
      could land on a third backend, leaking the lock FOREVER and deadlocking
      every future boot. That is strictly worse than the defect being fixed;
    * an open transaction pins its server connection for its whole life, so the
      xact lock is genuinely held across the run, and it is released by the
      backend the instant the transaction ends — including when the pod is killed
      mid-migration. There is no unlock to forget and no state to leak.

    THE COST, NAMED: ``CREATE INDEX CONCURRENTLY`` CANNOT RUN WHILE THIS IS HELD.
    CIC waits for every transaction in the database that was open when it started,
    and this one is open for the whole run — so a pending file containing CIC would
    wait for the lock that is waiting for it. Measured, not reasoned: with the lock
    transaction open, a CIC on an unrelated, freshly created table blocks in
    ``Lock/virtualxid`` until the lock transaction ends, and completes in ~10ms
    without it. ``apply_all`` therefore REFUSES such a pending file with a named
    error rather than hanging the boot for ever — see
    ``_reject_out_of_transaction_statements``, which also records why the two
    obvious alternatives are worse.

    ``SET LOCAL lock_timeout = 0`` is load-bearing: ``lock_timeout`` DOES apply to
    an advisory-lock wait, so inheriting a non-zero value from the role or server
    would abort the loser instead of making it queue — reintroducing the failure
    this function exists to remove. ``idle_in_transaction_session_timeout = 0``
    likewise: this connection is idle-in-transaction for the entire run by design,
    and a role-level timeout would silently drop the lock mid-migration.
    """
    with psycopg.connect(dsn) as lock_conn:
        try:
            lock_conn.execute("SET LOCAL lock_timeout = 0")
            lock_conn.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
            lock_conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            yield
        finally:
            # Ends the transaction => releases the lock. ROLLBACK rather than
            # COMMIT because this connection deliberately wrote nothing.
            lock_conn.rollback()


_TRACKING_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)


def _exec(conn, sql: str, params=None):
    """Run one SQL string on the autocommit connection; return the cursor."""
    return conn.execute(sql, params)


def _run_file(conn, path: Path, timeout_ms: int = 0) -> None:
    """Execute one .sql file statement-by-statement (autocommit). Raises on error."""
    statements = split_statements(path.read_text(encoding="utf-8"))
    for k, stmt in enumerate(statements, 1):
        try:
            _exec(conn, _with_lock_timeout(stmt, timeout_ms))
        except Exception as exc:
            raise RuntimeError(f"{path.name} stmt {k}: {exc}") from exc


def _stamp(conn, filename: str) -> None:
    _exec(
        conn,
        "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT (filename) DO NOTHING",
        (filename,),
    )


def _reject_out_of_transaction_statements(pending: list[Path]) -> str | None:
    """Name the first pending file that cannot run under the migration lock.

    ``CREATE INDEX CONCURRENTLY`` (and ``VACUUM``) refuse to run inside a
    transaction block AND wait for every transaction that was open when they
    started. ``migration_lock`` holds exactly such a transaction for the whole run,
    so the two cannot coexist: the file would block until the lock it is running
    under is released, which is never. A silent forever-hang at boot is the worst
    possible outcome, so this turns it into a named failure BEFORE anything runs.

    WHY NOT DROP THE LOCK FOR THAT ONE STATEMENT. The gap is exactly where the race
    lives: the file is not stamped yet, so the second replica takes the lock, sees
    it pending, and runs the same ``CREATE INDEX CONCURRENTLY IF NOT EXISTS``
    concurrently — whose existence check is a check-then-act too, and whose loser
    errors on the duplicate relation name. That trades one race for the same race.

    WHY NOT A SESSION-LEVEL LOCK INSTEAD (which CIC tolerates). Production reaches
    Postgres through pgbouncer in TRANSACTION pooling mode with the default
    ``server_reset_query_always = 0``, so session state is neither sticky nor
    reset: ``pg_advisory_lock`` would be taken on one server connection, the
    matching ``pg_advisory_unlock`` would very likely reach another, and the lock
    would sit held on a pooled backend until ``server_lifetime`` recycled it —
    blocking EVERY boot in the meantime. Strictly worse than the defect.

    The way forward for a migration that genuinely needs CIC is to give this runner
    a DIRECT, non-pooled DSN (a ``your-postgres-host:5432`` URL of its own), which
    is a deployment change and not this file's to make. Until then: use a plain
    ``CREATE INDEX``, or apply it by hand and stamp it.

    Returns the offending filename, or None.
    """
    for path in pending:
        for stmt in split_statements(path.read_text(encoding="utf-8")):
            if _MUST_RUN_OUTSIDE_TRANSACTION.search(stmt):
                return path.name
    return None


def apply_all(dsn: str, migrations_dir: Path = MIGRATIONS_DIR, sslmode: str | None = None) -> int:
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print(f"[apply_migrations] no .sql files in {migrations_dir}", flush=True)
        return 0
    if sslmode and "sslmode=" not in dsn:
        dsn = f"{dsn}{'&' if '?' in dsn else '?'}sslmode={sslmode}"
    timeout_ms = lock_timeout_ms()
    # EVERYTHING below runs under the platform-wide migration lock — the tracking
    # DDL and the `applied` read included. Reading the pending list outside it
    # would be the same check-then-act one level up: both replicas would compute
    # the same list and then serialise only on the individual DDL, which is
    # precisely the 42710 this closes. See ``migration_lock``.
    with migration_lock(dsn), psycopg.connect(dsn, autocommit=True) as conn:
        _exec(conn, _TRACKING_DDL)
        applied: set[str] = {r[0] for r in _exec(conn, "SELECT filename FROM schema_migrations")}

        # Baseline: first run against a schema that already has the app tables
        # (offering = migration 010, present on prod AND after create_all on a
        # fresh DB) → stamp every current file WITHOUT running it. Prod has them
        # hand-applied; a fresh DB got the full current schema from create_all and
        # has no data for the backfills to touch. Avoids replaying files that
        # aren't safe against later data (006's narrow CHECK vs 007 platform_only).
        if not applied:
            row = _exec(conn, "SELECT to_regclass('public.offering') IS NOT NULL").fetchone()
            if row and row[0]:
                for path in files:
                    _stamp(conn, path.name)
                print(
                    f"[apply_migrations] baselined {len(files)} files as applied "
                    "(existing schema) — future migrations run once",
                    flush=True,
                )
                return 0

        pending = [p for p in files if p.name not in applied]
        offender = _reject_out_of_transaction_statements(pending)
        if offender is not None:
            print(
                f"[apply_migrations] FAILED {offender}: contains a statement that cannot run "
                "inside a transaction block (CONCURRENTLY / VACUUM). It would wait for the "
                "migration lock's own transaction and never finish. Use a plain CREATE INDEX, "
                "or apply and stamp it by hand.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        for path in pending:
            try:
                _run_file(conn, path, timeout_ms)
            except RuntimeError as exc:
                print(f"[apply_migrations] FAILED {exc}", file=sys.stderr, flush=True)
                return 1
            _stamp(conn, path.name)
            print(f"[apply_migrations] applied {path.name}", flush=True)
    print(
        f"[apply_migrations] done — {len(pending)} applied, "
        f"{len(files) - len(pending)} already current",
        flush=True,
    )
    return 0


def run_from_env() -> int:
    """Apply migrations using DATABASE_URL / POSTGRES_SSLMODE from the environment.

    Shared by ``__main__`` and the app lifespan (main.py), so migrations run on
    every startup right after ``create_all`` — same DB config the app uses.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        print("[apply_migrations] ERROR: DATABASE_URL is required", file=sys.stderr)
        return 1
    dsn = url.replace("+psycopg", "")
    return apply_all(dsn, sslmode=os.getenv("POSTGRES_SSLMODE"))


def main(argv: list[str] | None = None) -> int:
    return run_from_env()


if __name__ == "__main__":
    raise SystemExit(main())
