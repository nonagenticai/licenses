"""Regression guard: introspect_token must not fan out into per-row SELECTs.

``/license/introspect`` is the platform auth hot path and it runs against a
postgres that is (in prod) reached over pgbouncer on a different node, so every
extra SQL statement is an extra cross-node round-trip. The handler was refactored
from 5 sequential SELECTs across 2 connections (token, license, app, plan, then a
tenant lookup on a *second* ``get_session()``) down to:

  * ONE joined SELECT for token -> license -> app -> plan (many-to-one joinedload)
  * ONE tenant SELECT for byo_mode, reused on the SAME session (no 2nd checkout)

These tests count the statements actually issued so a future accidental N+1
(e.g. dropping the ``joinedload`` options, or reintroducing a per-attribute lazy
load) fails loudly instead of silently regressing hot-path latency.

Note on connection count: the prod engine uses ``NullPool`` so the removed second
``get_session()`` was a second physical (cross-node) connection; the test engine
uses the default pool, so we assert the meaningful, portable invariant — the
number of SELECTs — rather than a pool-implementation-specific connection count.
"""

from __future__ import annotations

# The counter itself moved to tests/support/sql_counter.py when /health grew a
# zero-SQL assertion; one implementation, two callers. Kept under the old local
# name so the assertions below read unchanged.
from tests.support.sql_counter import count_statements as _count_statements


class TestIntrospectQueryCount:
    def test_cold_introspect_issues_two_selects_and_one_update(self, db_manager, seed_token):
        """A cold introspect (stale/None last_used_at) = 2 SELECTs + 1 throttled UPDATE.

        SELECT #1: token joined to license, app, plan.
        SELECT #2: tenant row for byo_mode (same session).
        UPDATE #1: the throttled last_used_at write (first call always writes).
        """
        _token, raw = seed_token

        with _count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)

        assert result["active"] is True, rec.statements
        assert rec.verbs["select"] == 2, (
            f"expected exactly 2 SELECTs (joined token+license+app+plan, then byo tenant), "
            f"got {rec.verbs['select']}:\n" + "\n".join(rec.statements)
        )
        assert rec.verbs["update"] == 1, (
            f"expected exactly 1 UPDATE (throttled last_used_at), got {rec.verbs['update']}:\n"
            + "\n".join(rec.statements)
        )

    def test_warm_introspect_is_two_selects_no_write(self, db_manager, seed_token):
        """A throttled (warm) introspect stays read-only: 2 SELECTs, 0 writes.

        The first call primes last_used_at; the second falls inside the throttle
        window so no UPDATE is issued — proving the hot poll path is pure reads.
        """
        _token, raw = seed_token

        # Prime last_used_at (first call always writes).
        assert db_manager.introspect_token(raw)["active"] is True

        with _count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)

        assert result["active"] is True, rec.statements
        assert rec.verbs["select"] == 2, (
            f"expected exactly 2 SELECTs on the warm path, got {rec.verbs['select']}:\n"
            + "\n".join(rec.statements)
        )
        assert rec.verbs["update"] == 0, (
            f"expected 0 writes on the throttled warm path, got {rec.verbs['update']}:\n"
            + "\n".join(rec.statements)
        )
