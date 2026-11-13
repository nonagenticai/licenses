"""``DatabaseManager.__init__``'s engine construction must stay verified.

WHY THIS FILE EXISTS
--------------------
``tests/conftest.py`` builds the manager with ``DatabaseManager.__new__(DatabaseManager)``
and hand-assigns ``engine`` / ``SessionLocal``. That is what keeps the suite fast and
lets it point at a scratch database — but it also means the real ``__init__`` body
**never executes** during the 1100+ test suite. Every argument to ``create_engine``
was therefore unverifiable by CI: deleting one keeps the suite green and changes
production.

Two of those arguments are load-bearing:

* ``poolclass=NullPool`` — pgbouncer in transaction mode IS the pool; an app-side
  pool layered on top breaks across pgbouncer's per-transaction assignment.
* ``connect_args={"connect_timeout": …}`` — bounds a connect against a peer that
  BLACKHOLES (drops SYNs without an RST) instead of refusing.

The second one is not theoretical. Measured 2026-08-11 in a scratch namespace: with
no ``connect_timeout``, a connect to a blackholed pgbouncer blocked **~130 seconds**.
Because ``NullPool`` opens a fresh connection per session, that stall is paid on every
call — which is what turned gitops #2553 (readiness → ``/health/ready``) into a
CrashLoopBackOff of BOTH replicas, and got it reverted (#2556).

These tests run the REAL construction path with ``create_engine`` captured, so its
arguments are pinned.

⚠️ Every assertion below is mutation-verified: removing the kwarg it pins turns it
RED. A pin that does not fail when the thing it pins is removed is worth nothing.
Modelled on ``provisioning/tests/test_db_engine_config.py``, the fleet's existing
shape for this problem.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.pool import NullPool

import models


class _SentinelEngine:
    """Stands in for the Engine so no real connection is ever opened here."""


@pytest.fixture
def captured_engine_kwargs(monkeypatch) -> dict[str, Any]:
    """Run the REAL ``DatabaseManager.__init__`` and capture ``create_engine``'s args."""
    captured: dict[str, Any] = {}

    def _fake_create_engine(url: str, **kwargs: Any) -> _SentinelEngine:
        captured["url"] = url
        captured.update(kwargs)
        return _SentinelEngine()

    monkeypatch.setattr(models, "create_engine", _fake_create_engine)
    # sessionmaker would otherwise try to bind to the sentinel.
    monkeypatch.setattr(models, "sessionmaker", lambda **kw: kw)
    # Keep the SSL helper from reaching the filesystem for certs.
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")

    models.DatabaseManager(database_url="postgresql+psycopg://u:p@example.invalid:5432/licenses")

    assert captured, "positive control: create_engine was never called"
    return captured


def test_engine_sets_a_bounded_connect_timeout(captured_engine_kwargs):
    """An unbounded connect is a ~130s stall per call under NullPool.

    Must stay comfortably under the deadlines that actually exist: the k8s
    readinessProbe timeout (3s), the livenessProbe timeout (5s), and
    license-authz's 3.0s introspect budget. A healthy in-cluster connect is
    single-digit ms, so this only fires on a genuine hang.
    """
    assert captured_engine_kwargs["connect_args"] == {
        "connect_timeout": models.DB_CONNECT_TIMEOUT_SECONDS
    }
    assert 0 < models.DB_CONNECT_TIMEOUT_SECONDS <= 3, (
        "connect_timeout must stay under the 3s readiness-probe and introspect budgets"
    )


def test_engine_uses_nullpool_because_pgbouncer_is_the_pool(captured_engine_kwargs):
    """An app-side pool on top of pgbouncer transaction mode breaks across its
    per-transaction server assignment."""
    assert captured_engine_kwargs["poolclass"] is NullPool


# NOT TESTED HERE, deliberately: that DB_CONNECT_TIMEOUT_SECONDS honours its
# environment override. The only way to exercise a module-scope os.getenv is to
# reload the module, and `importlib.reload(models)` REBINDS every ORM class —
# after which `isinstance` checks and relationship registries in already-imported
# modules (main.db_manager, the conftest fixtures) refer to the pre-reload
# classes. Measured: adding that one test turned 32 otherwise-passing tests RED
# across test_offerings/test_tenants/etc. while the suite was green without it.
# The override is a one-line os.getenv visible at the definition; pinning it is
# not worth breaking suite-wide ORM identity.


class TestSslModeIsNotDuplicated:
    """`_append_ssl_params` silently overrode an sslmode already in the DSN.

    It appended `sslmode=$POSTGRES_SSLMODE` unconditionally, so a DSN that
    already carried one came out with the key twice —
    `?sslmode=disable&…&sslmode=require`. libpq takes the LAST occurrence, so
    the operator's explicit value was replaced by the env var.

    `docker-compose.yml` ships exactly that combination: the DSN says
    `sslmode=disable` and `POSTGRES_SSLMODE` said `require`. Against a
    pgbouncer that is not serving TLS this is a hard connect failure produced
    by a DSN that reads as though it should work.

    Production is unaffected and was verified live: `POSTGRES_SSLMODE=disable`
    (so the function returns before this code) and its DSN carries no sslmode.
    """

    def _manager(self):
        from models import DatabaseManager

        return DatabaseManager.__new__(DatabaseManager)

    def test_an_sslmode_already_in_the_dsn_is_not_duplicated(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_SSLMODE", "require")
        dsn = "postgresql+psycopg://u@h:6432/db?sslmode=disable"
        result = self._manager()._append_ssl_params(dsn)
        assert result.count("sslmode=") == 1, result

    def test_the_dsn_value_wins_over_the_env_var(self, monkeypatch):
        """Asserts the EFFECTIVE value libpq would use, not just the count."""
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setenv("POSTGRES_SSLMODE", "require")
        dsn = "postgresql+psycopg://u@h:6432/db?sslmode=disable"
        result = self._manager()._append_ssl_params(dsn)
        effective = conninfo_to_dict(result.replace("postgresql+psycopg", "postgresql"))
        assert effective["sslmode"] == "disable", result

    def test_a_dsn_without_sslmode_still_gets_the_env_value(self, monkeypatch):
        """The env var stays the DEFAULT — this must not become a no-op."""
        monkeypatch.setenv("POSTGRES_SSLMODE", "require")
        for var in ("PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"):
            monkeypatch.delenv(var, raising=False)
        result = self._manager()._append_ssl_params("postgresql+psycopg://u@h:6432/db")
        assert "sslmode=require" in result, result
