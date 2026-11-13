"""Two replicas booting together must not race the migration runner.

THE DEFECT
----------
``migrations/021_add_tenants_byo_mode_check.sql`` asserts in its own header that
"this file must not be able to fail: the DO block is guarded on ``pg_constraint``,
so a re-run is a no-op" — and names ``replicas: 2`` in the same sentence. The
guard makes a re-run a no-op SERIALLY. It is a check-then-act, and the runner took
no lock of any kind, so two replicas booting concurrently both computed the same
pending list, both read the guard as false, and the second ``ADD CONSTRAINT``
raised ``42710 duplicate_object``.

That is not cosmetic. ``_run_file`` raises -> ``apply_all`` returns 1 ->
``main._run_db_bootstrap`` raises ``MigrationError`` -> ``is_db_unreachable`` is
False -> the lifespan re-raises -> uvicorn exits: CrashLoopBackOff on one replica
of the platform's ext_authz service. 021 is already stamped in prod, so THAT
file's window is shut; the defect is a CLASS one and applies to every migration
after it.

HOW THE RACE IS MADE DETERMINISTIC. No replicas are spun and nothing is timed.
The pending migration's FIRST statement parks on a second advisory lock this test
holds, which pins the winner mid-run with nothing stamped yet — exactly the window
the losing replica used to walk into. The test then waits for the loser to be
demonstrably blocked (on the test's lock if the runner is unserialised, on the
MIGRATION lock if it is) before releasing anything.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from sqlalchemy import text

from scripts.apply_migrations import (
    LOCK_TIMEOUT_ENV,
    MIGRATION_LOCK_KEY,
    _with_lock_timeout,
    apply_all,
)
from tests.conftest import TEST_DATABASE_URL

#: Advisory key the TEST holds to park a migration mid-run. Must not collide with
#: MIGRATION_LOCK_KEY (asserted below).
PARK_KEY = 918273645

POLL_TIMEOUT_S = 20.0


def _dsn() -> str:
    return TEST_DATABASE_URL.replace("+psycopg", "")


def _lock_parts(key: int) -> tuple[int, int]:
    """Split a bigint advisory key the way ``pg_locks`` reports it."""
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


def _waiters(conn, key: int) -> int:
    classid, objid = _lock_parts(key)
    return conn.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND classid = %s AND objid = %s AND objsubid = 1 "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
        (classid, objid),
    ).fetchone()[0]


def _wait_until(predicate, what: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _Runner(threading.Thread):
    def __init__(self, migrations_dir):
        super().__init__(daemon=True)
        self.migrations_dir = migrations_dir
        self.rc: int | None = None

    def run(self) -> None:
        self.rc = apply_all(_dsn(), migrations_dir=self.migrations_dir, sslmode="disable")


@pytest.fixture
def observer():
    """A connection outside the runners, for reading pg_locks and holding PARK_KEY."""
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        yield conn


@pytest.fixture
def baselined(db_engine, tmp_path):
    """A migrations dir already baselined, so the NEXT file genuinely runs."""
    with db_engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS schema_migrations"))
        c.execute(text("DROP TABLE IF EXISTS mig_race_marker"))
    (tmp_path / "001_baseline.sql").write_text("SELECT 1;")
    assert apply_all(_dsn(), migrations_dir=tmp_path, sslmode="disable") == 0
    yield tmp_path
    # Bounded teardown. Every runner here is a daemon thread; if an assertion above
    # fires while one is still mid-migration it keeps its connection, and an
    # unbounded DROP would queue behind it and wedge the whole pytest session into
    # an unattributable runner kill instead of one named failure.
    with db_engine.begin() as c:
        c.execute(text("SET LOCAL lock_timeout = '2s'"))
        for table in ("mig_race_marker", "schema_migrations"):
            try:
                c.execute(text(f"DROP TABLE IF EXISTS {table}"))
            except Exception:  # pragma: no cover - only reachable on a failed test
                c.rollback()
                break


def test_park_key_does_not_collide_with_the_migration_lock():
    assert PARK_KEY != MIGRATION_LOCK_KEY


class TestConcurrentRunnersDoNotCollide:
    def test_second_runner_waits_and_finds_nothing_pending(
        self, db_engine, observer, baselined, monkeypatch
    ):
        """Two callers, one lock: both succeed and the non-idempotent DDL runs ONCE.

        ``CREATE TABLE`` without ``IF NOT EXISTS`` stands in for 021's
        ``ADD CONSTRAINT``: both are check-then-act at the catalog level and both
        raise on the second concurrent execution. Unserialised, the loser returns 1.
        """
        # The runner's own lock_timeout would abort the deliberate park below
        # (lock_timeout DOES apply to advisory-lock waits), so disable it here —
        # this test is about the advisory lock, not the timeout.
        monkeypatch.setenv(LOCK_TIMEOUT_ENV, "0")

        (baselined / "002_race.sql").write_text(
            f"SELECT pg_advisory_lock({PARK_KEY});\n"
            "CREATE TABLE mig_race_marker (id int);\n"
            f"SELECT pg_advisory_unlock({PARK_KEY});\n"
        )

        observer.execute("SELECT pg_advisory_lock(%s)", (PARK_KEY,))
        first, second = _Runner(baselined), _Runner(baselined)
        try:
            first.start()
            _wait_until(
                lambda: _waiters(observer, PARK_KEY) >= 1,
                "the first runner to park inside its migration (un-stamped)",
            )

            second.start()
            # Either the second runner is queued on the MIGRATION lock (serialised,
            # the fixed behaviour) or it walked straight into the same migration and
            # is parked on PARK_KEY too (unserialised, the defect). Both are
            # observable states, so nothing here depends on timing.
            _wait_until(
                lambda: (
                    _waiters(observer, MIGRATION_LOCK_KEY) >= 1 or _waiters(observer, PARK_KEY) >= 2
                ),
                "the second runner to block",
            )
            assert _waiters(observer, PARK_KEY) < 2, (
                "both runners entered the same pending migration concurrently — "
                "the pending list is being computed outside any lock"
            )
        finally:
            observer.execute("SELECT pg_advisory_unlock(%s)", (PARK_KEY,))
            first.join(timeout=POLL_TIMEOUT_S)
            second.join(timeout=POLL_TIMEOUT_S)

        assert not first.is_alive() and not second.is_alive()
        assert first.rc == 0
        assert second.rc == 0, "the loser must WAIT and find nothing pending, not fail"

        with db_engine.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.mig_race_marker')")).scalar_one()
            stamped = c.execute(
                text("SELECT count(*) FROM schema_migrations WHERE filename = '002_race.sql'")
            ).scalar_one()
        assert stamped == 1


class TestLockTimeout:
    def test_a_blocked_ddl_fails_fast_instead_of_hanging_the_boot(
        self, db_engine, baselined, monkeypatch
    ):
        """A migration queueing behind someone else's ACCESS EXCLUSIVE lock aborts.

        Without a ``lock_timeout`` the runner waits for ever — and because a queued
        ACCESS EXCLUSIVE request blocks every reader that arrives behind it, that
        wait stalls ``introspect_token`` on the platform-wide auth hot path for as
        long as it lasts, with no error naming the cause.
        """
        monkeypatch.setenv(LOCK_TIMEOUT_ENV, "250")
        with db_engine.begin() as c:
            c.execute(text("CREATE TABLE mig_race_marker (id int)"))
        (baselined / "002_alter.sql").write_text(
            "ALTER TABLE mig_race_marker ADD COLUMN added_by_migration int;"
        )

        blocker = psycopg.connect(_dsn())
        try:
            blocker.execute("LOCK TABLE mig_race_marker IN ACCESS EXCLUSIVE MODE")
            runner = _Runner(baselined)
            runner.start()
            runner.join(timeout=15)
            assert not runner.is_alive(), (
                "the migration is still waiting on the table lock — no lock_timeout is set"
            )
            assert runner.rc == 1
        finally:
            blocker.rollback()
            blocker.close()

        # It failed rather than half-applied: nothing was stamped.
        with db_engine.connect() as c:
            stamped = c.execute(
                text("SELECT count(*) FROM schema_migrations WHERE filename = '002_alter.sql'")
            ).scalar_one()
        assert stamped == 0

    def test_statements_that_cannot_run_in_a_transaction_are_left_bare(self):
        """The timeout is prepended in the SAME simple query as the statement it
        guards, which puts that statement in an implicit transaction — so the
        statements that cannot tolerate one must never be prefixed."""
        concurrently = "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c)"
        assert _with_lock_timeout(concurrently, 5000) == concurrently
        assert _with_lock_timeout("VACUUM ANALYZE t", 5000) == "VACUUM ANALYZE t"

        ordinary = "ALTER TABLE t ADD COLUMN c int"
        assert _with_lock_timeout(ordinary, 5000) == f"SET lock_timeout = 5000;\n{ordinary}"
        assert _with_lock_timeout(ordinary, 0) == ordinary


class TestOutOfTransactionStatementsAreRefusedNotHung:
    """The named cost of serialising the run.

    ``CREATE INDEX CONCURRENTLY`` waits for every transaction open when it starts,
    and the migration lock IS such a transaction for the whole run — so a pending
    file containing one would wait on the lock it runs under, for ever. A boot that
    hangs silently is the worst outcome available, so the runner refuses first.
    """

    def test_a_pending_concurrently_file_fails_fast(self, db_engine, baselined, capsys):
        (baselined / "002_cic.sql").write_text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_mig_race_marker ON mig_race_marker (id);"
        )
        with db_engine.begin() as c:
            c.execute(text("CREATE TABLE mig_race_marker (id int)"))

        runner = _Runner(baselined)
        runner.start()
        runner.join(timeout=15)
        assert not runner.is_alive(), "the runner hung instead of refusing"
        assert runner.rc == 1
        assert "CONCURRENTLY" in capsys.readouterr().err

    def test_nothing_is_stamped_or_half_applied_by_the_refusal(self, db_engine, baselined):
        (baselined / "002_cic.sql").write_text(
            "CREATE TABLE mig_race_marker (id int);\n"
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_mig_race_marker ON mig_race_marker (id);"
        )
        # Threaded with a bounded join, like the test above: if the refusal is ever
        # removed, the CIC waits on the migration lock's transaction FOR EVER, and
        # a direct call would hang the whole suite instead of failing this test.
        runner = _Runner(baselined)
        runner.start()
        runner.join(timeout=15)
        assert not runner.is_alive(), "the runner hung instead of refusing"
        assert runner.rc == 1
        with db_engine.connect() as c:
            # Refused BEFORE the first statement ran — not part-way through.
            assert (
                c.execute(text("SELECT to_regclass('public.mig_race_marker')")).scalar_one() is None
            )
            stamped = c.execute(
                text("SELECT count(*) FROM schema_migrations WHERE filename = '002_cic.sql'")
            ).scalar_one()
        assert stamped == 0

    def test_an_already_stamped_concurrently_file_is_not_refused(self, db_engine, baselined):
        """008 uses CONCURRENTLY and is stamped everywhere. Only PENDING files are
        checked, so the corpus as it stands still boots."""
        (baselined / "002_cic.sql").write_text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nope ON nope (id);"
        )
        with db_engine.begin() as c:
            c.execute(text("INSERT INTO schema_migrations (filename) VALUES ('002_cic.sql')"))
        runner = _Runner(baselined)
        runner.start()
        runner.join(timeout=15)
        assert not runner.is_alive()
        assert runner.rc == 0
