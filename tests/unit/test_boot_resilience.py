"""A database that is DOWN at boot must not CrashLoopBackOff the platform auth pod.

THE DEFECT THIS EXISTS FOR
--------------------------
``lifespan`` ran ``create_tables()`` bare, so an unreachable database at startup
raised straight out of it: uvicorn exits, kubelet restarts, backoff grows. This
service backs ext_authz for the whole platform at ``replicas: 2``, so a database
blip that killed one container took the pod into CrashLoopBackOff and kept it
there. MEASURED 2026-08-11 in scratch namespace ``licenses-verify2``: postgres
had been back for 40 SECONDS and the pod was still CrashLoopBackOff at 5
restarts. Fail-fast delayed the recovery it existed to protect.

THE RULE, AND WHY IT IS TWO-SIDED
---------------------------------
Fail-fast is RIGHT for a broken migration and WRONG for a transient outage. A
fix that only removes the fail-fast is a regression: it would let a pod serve
traffic against a schema that failed to migrate. So the pair of tests that
matter here are adversarial to each other —
``test_lifespan_survives_transient_db_outage`` and
``test_lifespan_still_fails_fast_on_a_bad_migration`` — and a change that
satisfies one by breaking the other is exactly what this file exists to catch.

The two cases are distinguished STRUCTURALLY, not by heuristics:
``scripts/apply_migrations.apply_all`` opens its own ``psycopg.connect``, so an
unreachable database RAISES, while a migration that ran and failed is caught
there and RETURNS 1.

⚠️ THE VACUITY TRAP IN THIS FILE. ``/health/ready`` returns 503 both when the
bootstrap is pending AND when ``SELECT 1`` fails, so a test that asserts only
"503" cannot tell the new gate from the pre-existing connectivity check. Every
readiness test below therefore pins the flag against a HEALTHY database, so the
only thing that can produce the 503 is the gate under test.
"""

from __future__ import annotations

import asyncio
import logging

import psycopg
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

import main
from models import is_db_unreachable


@pytest.fixture
def _point_main_at_test_db(db_manager, monkeypatch):
    """Point ``main`` at the test database AND restore the readiness flag.

    ``app.state`` is module-level state on a shared ``main.app``; a test that
    left ``db_bootstrap_ok`` False would make every later readiness test fail
    for the wrong reason.
    """
    monkeypatch.setattr(main, "db_manager", db_manager)
    original = main.app.state.db_bootstrap_ok
    yield db_manager
    main.app.state.db_bootstrap_ok = original


def _sqlalchemy_connect_failure() -> OperationalError:
    """The shape SQLAlchemy raises when the connect itself fails.

    ``orig`` is a real ``psycopg.OperationalError`` because that is what the
    driver raises for 'connection failed', and because ``_is_retryable_db_fault``
    inspects ``orig`` to exclude statement timeouts.
    """
    return OperationalError(
        "SELECT 1", {}, psycopg.OperationalError("connection failed: server closed the connection")
    )


class TestIsDbUnreachable:
    """The predicate that forks transient from permanent. Wrong here = wrong everywhere."""

    def test_raw_psycopg_operational_error_is_unreachable(self):
        """``apply_all`` uses raw psycopg, NOT SQLAlchemy — this is the gap that arm closes."""
        assert is_db_unreachable(psycopg.OperationalError("connection refused")) is True

    def test_raw_psycopg_interface_error_is_unreachable(self):
        """DBAPI makes InterfaceError a SIBLING of DatabaseError, not a subclass."""
        assert is_db_unreachable(psycopg.InterfaceError("connection already closed")) is True

    def test_sqlalchemy_connect_failure_is_unreachable(self):
        assert is_db_unreachable(_sqlalchemy_connect_failure()) is True

    def test_sqlalchemy_interface_error_is_unreachable(self):
        assert is_db_unreachable(InterfaceError("SELECT 1", {}, Exception("closed"))) is True

    def test_query_canceled_is_not_unreachable(self):
        """THE SUBCLASSING TRAP.

        ``psycopg.errors.QueryCanceled`` subclasses ``psycopg.OperationalError``,
        so the ``_RAW_PSYCOPG_CONNECT_FAULTS`` arm would swallow it by default and
        omission alone could never exclude it. A statement timeout means the
        server ANSWERED and was slow — the database is reachable, so treating it
        as 'unreachable' would defer a boot that should have failed fast.
        """
        assert isinstance(psycopg.errors.QueryCanceled("timeout"), psycopg.OperationalError), (
            "precondition of this test: QueryCanceled must subclass OperationalError"
        )
        assert is_db_unreachable(psycopg.errors.QueryCanceled("statement timeout")) is False

    def test_migration_error_is_not_unreachable(self):
        """This is what keeps fail-fast alive for the case it is right for."""
        assert is_db_unreachable(main.MigrationError("migration 013 failed")) is False

    def test_unrelated_exception_is_not_unreachable(self):
        assert is_db_unreachable(ValueError("nothing to do with the database")) is False


class TestLifespanBootstrap:
    async def test_healthy_boot_reports_ready(self, _point_main_at_test_db):
        """POSITIVE CONTROL — without this, the tests below could pass vacuously.

        If the flag were never set True by ANY path, 'not ready when the database
        is down' would be trivially true and would prove nothing.
        """
        main.app.state.db_bootstrap_ok = False

        async with main.lifespan(main.app):
            assert main.app.state.db_bootstrap_ok is True, (
                "a healthy boot must report ready — if this fails, every other "
                "assertion in this file is vacuous"
            )

    async def test_lifespan_survives_transient_db_outage(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """THE FIX. An unreachable database must NOT raise out of lifespan.

        Mutants that turn this RED: restore the bare ``db_manager.create_tables()``
        call in ``lifespan`` (i.e. drop the try/except entirely); or drop
        lifespan's ``db_bootstrap_ok = False`` reset.

        The flag is seeded TRUE below on purpose. The deferred branch only leaves
        it alone, so without lifespan's explicit reset a pod that had booted
        successfully earlier in the same process would report READY while this
        bootstrap was failing. Seeding it makes that order-INDEPENDENT: the
        original version of this test inherited False from a fresh module and
        passed in isolation while failing in the full suite.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 3600)
        main.app.state.db_bootstrap_ok = True

        def _boom():
            raise _sqlalchemy_connect_failure()

        monkeypatch.setattr(main.db_manager, "create_tables", _boom)

        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            # Entering at all IS the assertion: before this change the connect
            # failure propagated and uvicorn exited.
            async with main.lifespan(main.app):
                assert main.app.state.db_bootstrap_ok is False, (
                    "the pod must serve NOT READY until the schema is confirmed"
                )

        assert any("db_bootstrap_deferred" in r.message for r in caplog.records), (
            "a deferred bootstrap must be visible in the log — it is the only "
            "signal an operator gets that the pod is alive but not serving"
        )

    async def test_lifespan_still_fails_fast_on_a_bad_migration(
        self, _point_main_at_test_db, monkeypatch
    ):
        """THE GUARD ON THE FIX. A migration that RAN and FAILED is still fatal.

        Without this, 'survive a transient outage' degenerates into 'never fail',
        and a pod would serve ext_authz against a half-migrated schema.

        Mutant that turns this RED: swallow the non-zero return code (or widen
        ``is_db_unreachable`` to include ``MigrationError``).
        """
        import scripts.apply_migrations

        monkeypatch.setattr(scripts.apply_migrations, "run_from_env", lambda: 1)

        with pytest.raises(main.MigrationError):
            async with main.lifespan(main.app):
                pytest.fail("lifespan must not yield when a migration failed")

    async def test_a_deferred_bootstrap_does_not_leak_its_retry_task(
        self, _point_main_at_test_db, monkeypatch
    ):
        """Shutdown must cancel the retry, or it outlives the executor it uses.

        The retry calls ``asyncio.to_thread``, which dispatches to the executor
        ``lifespan`` shuts down on exit. A task still in flight there is a task
        submitting to a closed pool.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 3600)
        monkeypatch.setattr(
            main.db_manager,
            "create_tables",
            lambda: (_ for _ in ()).throw(_sqlalchemy_connect_failure()),
        )

        before = {t for t in asyncio.all_tasks()}
        async with main.lifespan(main.app):
            spawned = {t for t in asyncio.all_tasks()} - before
            assert len(spawned) == 1, (
                f"expected exactly one retry task, saw {len(spawned)} — a changed "
                "count means this test is no longer measuring what it names"
            )
        assert all(t.done() for t in spawned), (
            "lifespan exited with the bootstrap retry still running; it would "
            "submit to the executor lifespan just shut down"
        )


@pytest.fixture(autouse=True)
def _reset_fresh_database_latch():
    """``main._fresh_database_pending_warning`` is module state that outlives a
    call by design (the retry needs it). Leaking it between tests would make one
    test's fresh database produce another test's warning."""
    main._fresh_database_pending_warning = False
    yield
    main._fresh_database_pending_warning = False


class TestTheFreshDatabaseProbe:
    """``database_is_pristine()`` must answer the question it NAMES, asked when
    it is still answerable.

    The whole point is that the migration runner's own probe cannot: by the time
    it looks, ``create_tables()`` has made the table it looks for. So the value
    of this probe rests entirely on *when* it runs, and a test that only stubs it
    proves nothing about that. The first tests below therefore use REAL databases
    and drive the real transitions.
    """

    @staticmethod
    def _scratch_manager(db_engine, name_prefix):
        """A DatabaseManager on a freshly CREATEd database, plus its cleanup.

        The engine is swapped for a plain one exactly as the ``db_manager``
        fixture does (conftest.py:247): ``DatabaseManager.__init__`` runs the DSN
        through ``_append_ssl_params``, which defaults to ``sslmode=require``
        when POSTGRES_SSLMODE is unset — and the CI archetype's ephemeral
        postgres speaks no SSL, so constructing one directly fails in CI while
        passing on any machine that happens to export POSTGRES_SSLMODE=disable.
        These tests are about the probe's SQL, not about DSN construction.
        """
        import uuid

        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool

        from models import DatabaseManager
        from tests.conftest import TEST_DATABASE_URL

        scratch = f"{name_prefix}_{uuid.uuid4().hex[:12]}"
        admin = db_engine.execution_options(isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{scratch}"'))
        scratch_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{scratch}"
        manager = DatabaseManager(scratch_url)
        manager.engine.dispose()
        manager.engine = create_engine(scratch_url, poolclass=NullPool)

        def cleanup():
            manager.engine.dispose()
            with admin.connect() as c:
                c.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :d AND pid <> pg_backend_pid()"
                    ),
                    {"d": scratch},
                )
                c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))

        return manager, cleanup

    def test_it_is_true_on_an_empty_database_and_false_after_create_all(self, db_engine):
        """The transition the whole PR rests on, measured rather than argued.

        Mutants that turn this RED: probe a table the ORM does not declare;
        return a constant; probe ``schema_migrations`` for the app-tables half
        (raw DDL, not an ORM table, so ``create_all`` never makes it and the
        answer would never flip).
        """
        from sqlalchemy import text

        manager, cleanup = self._scratch_manager(db_engine, "probe_fresh")
        try:
            with manager.engine.connect() as c:
                tables = c.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                ).scalar()
            assert tables == 0, (
                f"precondition of this test: the scratch database must really be "
                f"empty, found {tables} tables in public. Without this the first "
                f"assertion below would be asserting the thing under test."
            )

            assert manager.database_is_pristine() is True, (
                "a brand-new empty database must report PRISTINE — this is the "
                "exact case the migration runner's own probe gets wrong"
            )
            manager.create_tables()
            assert manager.database_is_pristine() is False, (
                "after create_all the answer must flip; if it does not, the probe "
                "is not measuring the app schema at all"
            )
        finally:
            cleanup()

    def test_a_dropped_schema_with_surviving_migration_history_is_not_pristine(self, db_engine):
        """``schema_migrations`` is raw DDL, so ``drop_all`` does NOT remove it.

        Such a database has no app tables but DOES have 21 stamps, so the runner's
        ``if not applied`` guard is False and it does NOT baseline. A probe that
        only asked "are the app tables gone?" would answer "pristine" and the
        caller would claim a baselining that never happened.

        Mutant that turns this RED: drop the migration-history half of the probe.
        """
        from sqlalchemy import text

        from models import Base

        manager, cleanup = self._scratch_manager(db_engine, "probe_dropall")
        try:
            manager.create_tables()
            with manager.engine.begin() as c:
                c.execute(
                    text(
                        "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                    )
                )
                c.execute(text("INSERT INTO schema_migrations (filename) VALUES ('001_x.sql')"))
            Base.metadata.drop_all(bind=manager.engine)

            with manager.engine.connect() as c:
                assert c.execute(text("SELECT to_regclass('public.license')")).scalar() is None, (
                    "precondition: drop_all must have removed the app tables"
                )
                surviving = c.execute(text("SELECT count(*) FROM schema_migrations")).scalar()
            assert surviving == 1, (
                "precondition of this test: schema_migrations must SURVIVE drop_all "
                "(it is not an ORM table). If this fails the test proves nothing."
            )

            assert manager.database_is_pristine() is False, (
                "app tables gone but migration history intact is NOT pristine — the "
                "runner will not baseline, so the fresh-database warning would be a "
                "false statement about what happened"
            )
        finally:
            cleanup()

    def test_a_fresh_database_warns_and_names_the_gate(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """A genuinely fresh database must SAY that its schema is ORM-only.

        Mutant that turns this RED: delete the ``if _fresh_database_pending_warning``
        body.
        """
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: True)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", lambda: 0)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)

        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            main._run_db_bootstrap()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("db_bootstrap_fresh_database" in r.message for r in warnings), (
            "a fresh database must warn — it is the only moment the system can say "
            "that the migrations were discarded"
        )
        message = next(r.message for r in warnings if "db_bootstrap_fresh_database" in r.message)
        assert "test_schema_parity" in message, (
            "the warning must NAME what carries the guarantee. A warning that says "
            "'something is unverified' without saying what verifies it sends the "
            "operator nowhere."
        )

    def test_a_populated_database_says_nothing(self, _point_main_at_test_db, monkeypatch, caplog):
        """THE OTHER HALF, and the one a 'does it log?' test omits.

        Every real pod boots against a populated database. If this warning fired
        there it would be permanent boot noise, and an operator would learn to
        ignore the one case it exists for.

        Mutant that turns this RED: make the warning unconditional.
        """
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: False)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", lambda: 0)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)

        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            main._run_db_bootstrap()

        assert not any("db_bootstrap_fresh_database" in r.message for r in caplog.records), (
            "a pre-existing schema must NOT warn; prod boots this path every time"
        )

    def test_the_warning_is_not_lost_when_the_database_blips_mid_bootstrap(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """The warning gets ONE chance in a database's life. Don't throw it away.

        A database blip between ``create_tables()`` and the end of the bootstrap
        is the exact fault class this whole boot path exists for. When it happens
        on a genuinely fresh database, the retry re-runs ``_run_db_bootstrap``
        byte-identically — and by then the app tables EXIST, so the probe answers
        "not pristine" and a naively-placed warning is lost FOREVER on a database
        that really was baselined.

        Mutants that turn this RED: never clear the latch (2 warnings — it becomes
        boot noise); drop the latch AND leave the warning below the migration
        runner (0 warnings).

        ⚠️ It does NOT catch moving the warning below the migration runner on its
        own — verified, it stays green — because the latch makes the placement
        irrelevant for THIS fault. The latch is covered by
        ``test_the_warning_survives_a_failure_inside_create_tables``. Recording
        that here rather than claiming a discriminating power this test lacks.
        """
        pristine = {"value": True}
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: pristine["value"])
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", lambda: 0)

        # Boot 1: fresh database, then the migration step meets a dead socket.
        def _blip():
            raise _sqlalchemy_connect_failure()

        monkeypatch.setattr("scripts.apply_migrations.run_from_env", _blip)
        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            with pytest.raises(OperationalError):
                main._run_db_bootstrap()

            # Boot 2 is the retry. create_all already ran, so the database is no
            # longer pristine — this is what used to swallow the warning.
            pristine["value"] = False
            monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)
            main._run_db_bootstrap()

        warned = [r for r in caplog.records if "db_bootstrap_fresh_database" in r.message]
        assert len(warned) == 1, (
            f"the fresh-database warning must survive a mid-bootstrap blip and fire "
            f"exactly once across the boot and its retry, saw {len(warned)}. Zero "
            f"means it was lost on the database it existed for; more than one means "
            f"the latch never clears and it becomes boot noise."
        )

    def test_the_warning_survives_a_failure_inside_create_tables(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """THE LATCH's actual reason to exist, and the window the test above misses.

        ``create_all`` is not atomic. If it dies part-way through a genuinely fresh
        database, some tables exist — so the retry's probe answers "not pristine"
        while the database really was empty and really will be baselined. Only a
        fact that OUTLIVES the call that observed it can still warn there.

        Mutant that turns this RED: make ``_fresh_database_pending_warning`` a
        per-call local instead of module state. (The blip test above does NOT
        catch that mutant — its warning has already fired by the time the fault
        lands, so it is green either way. Verified.)
        """
        pristine = {"value": True}
        created = {"n": 0}
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: pristine["value"])
        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", lambda: 0)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)

        def _create_tables():
            created["n"] += 1
            if created["n"] == 1:
                # Died part-way: some tables landed, so the next probe sees them.
                pristine["value"] = False
                raise _sqlalchemy_connect_failure()

        monkeypatch.setattr(main.db_manager, "create_tables", _create_tables)

        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            with pytest.raises(OperationalError):
                main._run_db_bootstrap()
            assert not any("db_bootstrap_fresh_database" in r.message for r in caplog.records), (
                "nothing was built yet, so there is nothing to warn about yet"
            )

            main._run_db_bootstrap()  # the retry

        warned = [r for r in caplog.records if "db_bootstrap_fresh_database" in r.message]
        assert len(warned) == 1, (
            f"the retry must still warn: the database WAS empty and its schema IS "
            f"now ORM-only, even though the probe can no longer see that. Saw "
            f"{len(warned)} warnings."
        )

    async def test_a_dead_database_at_the_probe_defers_rather_than_exits(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """The regression this change could have introduced, pinned.

        Moving the probe before ``create_tables()`` moves the FIRST database touch
        of the whole boot. If that connect raised outside lifespan's deferred
        branch, an unreachable database at startup would CrashLoopBackOff both
        replicas of platform authz again — the exact defect #122 exists to
        prevent, re-opened by a logging change.

        Mutant that turns this RED: call ``database_is_pristine()`` from ``lifespan``
        outside the try, or from module scope.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 3600)
        main.app.state.db_bootstrap_ok = True

        def _boom():
            raise _sqlalchemy_connect_failure()

        monkeypatch.setattr(main.db_manager, "database_is_pristine", _boom)

        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            async with main.lifespan(main.app):
                assert main.app.state.db_bootstrap_ok is False
        assert any("db_bootstrap_deferred" in r.message for r in caplog.records)


class TestTheSeedIsOnTheSamePathsAsEverythingElse:
    """WS-2: two paths that were correct by construction and never executed.

    The existing outage tests stub ``create_tables`` or ``_run_db_bootstrap``
    WHOLESALE, so control never reaches ``seed_platform_tenants`` in any of them.
    Both assertions below were true before this file was written; neither was
    proven, and 'true but unproven' is how the next refactor breaks them.
    """

    async def test_a_connect_fault_from_the_seed_defers_rather_than_exits(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """The seed is the LAST database touch, so it is the one most likely to
        meet a database that died mid-boot. A connect fault there is still a
        connect fault.

        Mutant that turns this RED: wrap the seed call in a bare
        ``except Exception: raise`` inside ``_run_db_bootstrap``.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 3600)
        main.app.state.db_bootstrap_ok = True
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: False)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)

        def _boom():
            raise _sqlalchemy_connect_failure()

        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", _boom)

        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            async with main.lifespan(main.app):
                assert main.app.state.db_bootstrap_ok is False, (
                    "a database that died at the seed must leave the pod NOT READY, "
                    "not exit — the seed is inside the deferred region for a reason"
                )
        assert any("db_bootstrap_deferred" in r.message for r in caplog.records)

    async def test_the_retry_actually_seeds(self, _point_main_at_test_db, monkeypatch):
        """Recovery must produce a SEEDED database, not merely a ready flag.

        ``_run_db_bootstrap``'s contract is that the retry re-runs it
        byte-identically; the seed lives inside it precisely so a pod that booted
        against a briefly-dead database still has the platform identities before
        it reports ready. Nothing asserted that. A test that only checks
        ``db_bootstrap_ok`` would pass with the seed deleted.

        Mutant that turns this RED: move ``seed_platform_tenants()`` out of
        ``_run_db_bootstrap`` and into ``lifespan``'s success branch — the flag
        still flips, and only this assertion notices.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 0.01)
        main.app.state.db_bootstrap_ok = False

        seeded = {"n": 0}
        attempts = {"n": 0}

        def _probe():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _sqlalchemy_connect_failure()
            return False

        monkeypatch.setattr(main.db_manager, "database_is_pristine", _probe)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)
        monkeypatch.setattr(
            main.db_manager,
            "seed_platform_tenants",
            lambda: seeded.__setitem__("n", seeded["n"] + 1) or 0,
        )

        await asyncio.wait_for(main._retry_db_bootstrap_until_ready(main.app), timeout=10)

        assert main.app.state.db_bootstrap_ok is True
        assert seeded["n"] == 1, (
            f"the recovered boot must seed exactly once, saw {seeded['n']} — a pod "
            "that recovers without seeding reports ready while the identities the "
            "fleet hardcodes are missing"
        )


class TestBootstrapRetry:
    async def test_retry_marks_ready_once_the_database_returns(
        self, _point_main_at_test_db, monkeypatch
    ):
        """The recovery half: the pod rejoins the EndpointSlice WITHOUT a restart.

        Mutant that turns this RED: never set ``db_bootstrap_ok`` in the retry.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 0.01)
        main.app.state.db_bootstrap_ok = False

        calls = {"n": 0}

        def _down_then_up():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _sqlalchemy_connect_failure()

        monkeypatch.setattr(main, "_run_db_bootstrap", _down_then_up)

        await asyncio.wait_for(main._retry_db_bootstrap_until_ready(main.app), timeout=10)

        assert calls["n"] == 3, (
            f"the retry must keep going while the database is down; it ran {calls['n']} times"
        )
        assert main.app.state.db_bootstrap_ok is True

    async def test_retry_stops_and_stays_unready_on_a_permanent_failure(
        self, _point_main_at_test_db, monkeypatch, caplog
    ):
        """Database back, migration genuinely broken: stop retrying, never report ready.

        Staying alive-but-unready beats exiting: with ``maxUnavailable: 0`` the
        previous pods keep serving and the rollout stalls visibly.
        """
        monkeypatch.setattr(main, "DB_BOOTSTRAP_RETRY_SECONDS", 0.01)
        main.app.state.db_bootstrap_ok = False

        calls = {"n": 0}

        def _down_then_broken():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _sqlalchemy_connect_failure()
            raise main.MigrationError("migration 013 failed")

        monkeypatch.setattr(main, "_run_db_bootstrap", _down_then_broken)

        with caplog.at_level(logging.CRITICAL, logger=main.logger.name):
            await asyncio.wait_for(main._retry_db_bootstrap_until_ready(main.app), timeout=10)

        assert main.app.state.db_bootstrap_ok is False, (
            "a broken migration must NEVER be retried into readiness"
        )
        assert calls["n"] == 2, "the retry must stop at the permanent failure, not spin"
        assert any("db_bootstrap_failed_permanently" in r.message for r in caplog.records), (
            "a pod that will never become ready must say so at CRITICAL — silence "
            "here is a rollout that stalls with no explanation"
        )


class TestReadinessGate:
    def test_ready_503_while_bootstrap_pending_even_though_the_db_is_up(self, client, monkeypatch):
        """CONNECTIVITY IS NOT SUFFICIENT — the schema has to exist too.

        The database here is HEALTHY (the client fixture just migrated it), so
        ``SELECT 1`` would succeed and the pre-existing check would report ready.
        The ONLY thing that can produce a 503 is the new gate — which is what
        stops a pod rejoining the EndpointSlice in the window where the database
        came back before the migrations ran.

        Mutant that turns this RED: drop the ``db_bootstrap_ok`` check.
        """
        original = main.app.state.db_bootstrap_ok
        try:
            main.app.state.db_bootstrap_ok = False
            response = client.get("/health/ready")
        finally:
            main.app.state.db_bootstrap_ok = original

        assert response.status_code == 503, (
            "a pod whose schema is unconfirmed must not be sent ext_authz traffic"
        )
        assert response.json()["detail"] == "Database not ready"

    def test_ready_200_when_bootstrap_completed(self, client):
        """The other half — proves the test above is not just 'readiness is broken'."""
        assert main.app.state.db_bootstrap_ok is True, (
            "the client fixture runs lifespan against a healthy database"
        )
        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "connected"}

    def test_liveness_stays_200_while_bootstrap_pending(self, client):
        """The whole point: NOT READY must not mean NOT ALIVE.

        If liveness failed here kubelet would kill the container and we would be
        back to CrashLoopBackOff — the defect this change exists to remove.
        """
        original = main.app.state.db_bootstrap_ok
        try:
            main.app.state.db_bootstrap_ok = False
            response = client.get("/health/live")
        finally:
            main.app.state.db_bootstrap_ok = original

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
