"""Tests for per-tenant BYO LLM entitlement (byo_mode)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event

from models import Tenant, TenantPosture, Token

SEED_TENANT = "01000000-0000-7000-8000-000000000001"  # matches seed_license fixture


@contextmanager
def _count_selects(engine):
    """Count the SELECTs issued on ``engine`` inside the block.

    Used to prove the retry adds no query on the SUCCESS path: a speculative
    second statement in ``_lookup_byo_mode`` is a round-trip on every token
    introspection fleet-wide.
    """
    rec = SimpleNamespace(selects=0, statements=[])

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        rec.statements.append(statement)
        if statement.lstrip().lower().startswith("select"):
            rec.selects += 1

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield rec
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)


def _make_tenant(db_manager, *, byo_mode="local_allowed", tenant_id=SEED_TENANT, name="byo-test"):
    session = db_manager.get_session()
    try:
        # merge (upsert by PK), not add: the autouse _register_seed_tenants fixture
        # already registers SEED_TENANT to satisfy the license.tenant_id FK, so a
        # raw insert of the same id would hit tenants_pkey. merge sets byo_mode.
        session.merge(
            Tenant(id=UUID(tenant_id), name=name, byo_mode=byo_mode, owner_key="platform")
        )
        session.commit()
    finally:
        session.close()


class TestSetTenantByoMode:
    def test_set_by_id(self, db_manager):
        _make_tenant(db_manager)
        t = db_manager.set_tenant_byo_mode(SEED_TENANT, "byo_required")
        assert t is not None and t.byo_mode == "byo_required"

    def test_set_by_name(self, db_manager):
        _make_tenant(db_manager, name="acme-corp")
        t = db_manager.set_tenant_byo_mode("acme-corp", "byo_required")
        assert t is not None and t.byo_mode == "byo_required"

    def test_set_platform_only(self, db_manager):
        _make_tenant(db_manager)
        t = db_manager.set_tenant_byo_mode(SEED_TENANT, "platform_only")
        assert t is not None and t.byo_mode == "platform_only"

    def test_invalid_value_raises(self, db_manager):
        _make_tenant(db_manager)
        with pytest.raises(ValueError, match="invalid byo_mode"):
            db_manager.set_tenant_byo_mode(SEED_TENANT, "bogus")

    def test_unknown_tenant_returns_none(self, db_manager):
        assert (
            db_manager.set_tenant_byo_mode("99999999-0000-7000-8000-000000000000", "byo_required")
            is None
        )

    def test_new_tenant_defaults_to_byo_required(self, db_manager):
        # Migration 058 (licenses#273, owner ruling 2026-10-15): a tenant created
        # without an explicit byo_mode gets 'byo_required' (new early-access tenants
        # bring their own tokens). It was 'local_allowed' under 042. Use a fresh id
        # (NOT the autoregistered SEED_TENANT) so this tests genuine creation.
        fresh_id = UUID("019e0f00-0000-7000-8000-0000000000d1")
        session = db_manager.get_session()
        try:
            session.add(Tenant(id=fresh_id, name="defaults-test", owner_key="platform"))
            session.commit()
            t = session.query(Tenant).filter(Tenant.id == fresh_id).first()
            assert t is not None and t.byo_mode == "byo_required"
        finally:
            session.close()


class TestIntrospectByoMode:
    def test_introspect_includes_byo_required(self, db_manager, seed_token):
        _make_tenant(db_manager, byo_mode="byo_required")
        _token, raw = seed_token
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["byo_mode"] == "byo_required"

    def test_introspect_fails_closed_without_tenant_row(self, db_manager, seed_token):
        # No Tenant row for the seed tenant_id -> fail-CLOSED to byo_required.
        # Every active-license tenant_id is backfilled with an explicit row, so a
        # missing row means an unprovisioned tenant, which gets no platform GPU.
        _token, raw = seed_token
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["byo_mode"] == "byo_required"

    def test_introspect_includes_platform_only(self, db_manager, seed_token):
        _make_tenant(db_manager, byo_mode="platform_only")
        _token, raw = seed_token
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["byo_mode"] == "platform_only"

    def test_introspect_last_used_at_survives_a_byo_fault(
        self, db_manager, seed_token, monkeypatch
    ):
        """The throttled ``last_used_at`` write must COMMIT before the byo lookup.

        Ordering invariant, not a new behaviour: ``introspect_token`` commits the
        stale-``last_used_at`` refresh *before* calling ``_lookup_byo_mode``, so a
        callee that ends the transaction cannot discard the write. Move the commit
        after the lookup and the rollback silently eats every touch — a write the
        hot path only makes once per throttle window, so the loss is invisible
        without this pin.
        """
        token, raw = seed_token
        stale = datetime.now(UTC) - timedelta(days=7)
        session = db_manager.get_session()
        try:
            row = session.query(Token).filter(Token.id == token.id).first()
            assert row is not None
            setattr(row, "last_used_at", stale)
            session.commit()
        finally:
            session.close()

        def _faulting_lookup(tenant_id, app_key=None, session=None):
            # Stand in for ANY callee that ends the transaction it was handed.
            # (The fixed _lookup_tenant_posture only rolls back a session it OWNS
            # — this stub is deliberately hostile so the pin survives that guard.)
            if session is not None:
                session.rollback()
            return TenantPosture(byo_mode="byo_required", archived=False, name=None)

        # ⚠️ PATCH TARGET IS _lookup_tenant_posture, NOT _lookup_byo_mode. The
        # introspect path stopped calling the latter when the archived gate began
        # needing the archived flag off the same row read. Patching the accessor
        # here would leave the real lookup running, the hostile rollback would
        # never fire, and this test would PASS while pinning nothing at all.
        monkeypatch.setattr(db_manager, "_lookup_tenant_posture", _faulting_lookup)

        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["byo_mode"] == "byo_required"

        verify = db_manager.get_session()
        try:
            fresh = verify.query(Token).filter(Token.id == token.id).first()
            assert fresh is not None
            assert fresh.last_used_at > stale, (
                "last_used_at was rolled back by the byo lookup — the throttled "
                f"commit must happen BEFORE it (still {fresh.last_used_at!r})"
            )
        finally:
            verify.close()

    def test_introspect_uses_tenant_wide_posture_ignoring_per_app_override(
        self, db_manager, seed_app, seed_token
    ):
        """introspect reports the TENANT-WIDE posture, never the per-product one.

        ``introspect_token`` calls ``_lookup_byo_mode`` WITHOUT ``app_key``, so a
        ``tenant_app_byo_mode`` override is deliberately not consulted here: the
        per-product posture is opt-in via the dedicated endpoint's ``?app``, and
        the override table is empty in prod. Making introspect honour the override
        would change the posture every ext_authz call sees and add a third SELECT
        to the auth hot path — that is a REVIEWED PRODUCT DECISION, not a bugfix.
        If you are here because you want per-app posture on introspect, change it
        deliberately and update this test's rationale; do not delete the test.
        """
        _make_tenant(db_manager, byo_mode="byo_required")
        override = db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        assert override is not None and override["byo_mode"] == "platform_only"

        _token, raw = seed_token
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["byo_mode"] == "byo_required", (
            "introspect must report the tenant-wide posture; a per-app override "
            "leaking into introspect changes what every ext_authz call sees"
        )


class TestLookupByoMode:
    def test_legacy_string_name_resolves(self, db_manager):
        _make_tenant(db_manager, byo_mode="local_allowed", name="legacy-name")
        assert db_manager._lookup_byo_mode("legacy-name") == "local_allowed"
        # Unknown tenant -> fail-CLOSED to byo_required (no platform GPU by default).
        assert db_manager._lookup_byo_mode("no-such-tenant") == "byo_required"

    def test_lookup_fails_closed_when_both_attempts_raise(self, db_manager, monkeypatch, caplog):
        """A PERSISTENT infra fault must fail CLOSED to byo_required, loudly.

        ``local_allowed`` is the MOST permissive value in the enum: it lets the
        tenant run inference on the platform owner's GPU, billed to nobody. So a
        DB error, a statement timeout, or an unmigrated column — anything that
        leaves the posture unresolvable — is a denial, exactly like the "no
        tenant row" policy signal. There is no asymmetry between them.

        Persistent means BOTH attempts raise: the first one and the bounded
        retry on a fresh session. Only then is the posture genuinely
        unresolvable; a fault that clears on the retry is a transient,
        connection-scoped blip and is covered by
        ``test_lookup_retries_once_on_a_fresh_session_and_returns_the_real_posture``.

        The WARNING assertions are part of the contract: failing closed must
        never degrade into denying *silently*, or an outage becomes
        indistinguishable from a legitimately BYO-required tenant. The WARNING
        must carry BOTH ``exc_info`` (what broke) and the TENANT ID (who was
        denied) — without the id an operator cannot tell a fleet-wide outage
        from one tenant's bad row, and cannot answer "was customer X denied?".
        """

        class _BoomSession:
            def query(self, *a, **k):
                raise RuntimeError("db down")

            def rollback(self):
                pass

            def close(self):
                pass

        opened: list[_BoomSession] = []

        def _boom():
            s = _BoomSession()
            opened.append(s)
            return s

        # A needle no other log line or fixture can produce, so a match proves
        # the record really interpolated THIS call's tenant_id.
        tenant = "tenant-needle-6f3a91c4"

        monkeypatch.setattr(db_manager, "get_session", _boom)
        with caplog.at_level(logging.WARNING, logger="models"):
            assert db_manager._lookup_byo_mode(tenant) == "byo_required"

        assert len(opened) == 2, (
            "expected TWO sessions — the first attempt plus the bounded retry on "
            f"a fresh one; got {len(opened)}"
        )

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "byo_mode lookup failed" in r.getMessage()
        ]
        assert warnings, (
            "the fail-closed path must emit a WARNING — a silent deny is "
            f"indistinguishable from a real byo_required tenant; got {caplog.records!r}"
        )
        assert warnings[0].exc_info is not None, (
            "the WARNING must carry exc_info, otherwise the underlying DB "
            "fault is unrecoverable from the logs"
        )
        assert tenant in warnings[0].getMessage(), (
            "the WARNING must name the tenant that was denied — an operator "
            "cannot triage 'is this everyone or just this tenant?' from a "
            f"message that omits it; got {warnings[0].getMessage()!r}"
        )

    def test_lookup_does_not_rollback_a_borrowed_session(self, db_manager):
        """A BORROWED session's transaction belongs to the caller, not to us.

        ``introspect_token`` passes its own live session in. Ending that
        transaction from a callee is a session-lifecycle violation: it expires
        the caller's identity map underneath it and discards whatever the
        caller's unit of work still had open. The fault must still be swallowed
        into a fail-CLOSED answer, so the call must NOT raise either.
        """

        class _BorrowedSession:
            def query(self, *a, **k):
                raise RuntimeError("db down")

            def rollback(self):
                raise AssertionError(
                    "a borrowed session must not be rolled back by the callee — "
                    "its transaction belongs to the caller's unit of work"
                )

            def close(self):
                raise AssertionError("a borrowed session must not be closed by the callee")

        assert (
            db_manager._lookup_byo_mode("any-tenant", session=_BorrowedSession()) == "byo_required"
        )

    def test_lookup_does_not_close_a_borrowed_session(self, db_manager):
        """The ``finally`` arm must close only a session we opened ourselves.

        Closing a borrowed session would return the caller's connection to the
        pool mid-request, so every statement the caller issues afterwards runs
        on a different (or dead) connection.
        """

        class _BorrowedSession:
            def __init__(self):
                self.rolled_back = 0

            def query(self, *a, **k):
                raise RuntimeError("db down")

            def rollback(self):
                self.rolled_back += 1

            def close(self):
                raise AssertionError(
                    "a borrowed session must not be closed by the callee — the "
                    "caller still needs the connection"
                )

        borrowed = _BorrowedSession()
        assert db_manager._lookup_byo_mode("any-tenant", session=borrowed) == "byo_required"
        assert borrowed.rolled_back == 0

    def test_lookup_rolls_back_and_closes_its_own_session(self, db_manager, monkeypatch):
        """A session we OWN must be rolled back AND closed on the fault path.

        The mirror of the two borrowed-session tests: skipping the rollback
        would hand a poisoned (aborted-transaction) connection back to the pool,
        and skipping the close would leak it.

        The double hands out a FRESH session per ``get_session()`` call, as the
        real one does. It used to return one shared object, which silently
        conflated the first attempt with the bounded retry's own session — this
        test asserts on the FIRST session only; the retry's session has its own
        test (``test_retry_uses_and_closes_a_session_this_function_owns``).
        """

        class _OwnBoomSession:
            def __init__(self):
                self.rolled_back = 0
                self.closed = 0

            def query(self, *a, **k):
                raise RuntimeError("db down")

            def rollback(self):
                self.rolled_back += 1

            def close(self):
                self.closed += 1

        opened: list[_OwnBoomSession] = []

        def _open():
            s = _OwnBoomSession()
            opened.append(s)
            return s

        monkeypatch.setattr(db_manager, "get_session", _open)

        assert db_manager._lookup_byo_mode("any-tenant", session=None) == "byo_required"
        assert opened, "the lookup must open a session when it is not given one"
        own = opened[0]
        assert own.rolled_back == 1, "an owned session must be rolled back on the fault path"
        assert own.closed == 1, "an owned session must be closed exactly once"


class TestLookupByoModeRetry:
    """The ONE bounded retry on the fault path of ``_lookup_byo_mode``.

    Why it exists: the ``except`` arm does not return an error, it FABRICATES a
    posture, and the fabricated value is a hard denial. In prod only 23 of 52
    tenants are genuinely ``byo_required``; ``worker_management`` caches the
    answer for 300s and ``requirements_management`` excludes the resulting 402
    from its retryable statuses, so a one-second connection blip on the auth hot
    path becomes five minutes of hard denial for the other 29 tenants.

    Why it is sound: the arm is genuinely reachable while ``introspect_token``
    is HEALTHY — introspect runs the token->license->app->plan join FIRST and
    the byo SELECT LAST, so a fault here with the caller still holding a live
    result is statement- or connection-scoped, not "the database is down". A
    PERSISTENT fault raises on the fresh session too and still fails CLOSED
    (``test_lookup_fails_closed_when_both_attempts_raise``); the retry only
    separates the two cases, it never softens the deny.
    """

    def test_retry_on_a_fresh_session_returns_the_real_stored_posture(self, db_manager, caplog):
        """A transient first failure must NOT be laundered into a denial.

        This is the whole point of the seam: the tenant really is
        ``local_allowed``, the first attempt hit a dead connection, and the
        answer the caller gets must be the STORED posture — not the
        ``byo_required`` the fail-closed arm would otherwise fabricate.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")

        class _DeadConnectionSession:
            """The caller's session; its connection died mid-request."""

            def query(self, *a, **k):
                raise RuntimeError("server closed the connection unexpectedly")

            def rollback(self):
                raise AssertionError("a borrowed session must not be rolled back by the callee")

            def close(self):
                raise AssertionError("a borrowed session must not be closed by the callee")

        # Track close() on whatever real session the retry opens, so the
        # "always closes it" half of the contract is pinned on the SUCCESS path
        # too, not only on the double-failure path.
        original_get_session = db_manager.get_session
        closed: list[int] = []

        def _tracked_get_session():
            sess = original_get_session()
            real_close = sess.close

            def _close(*a, **k):
                closed.append(1)
                return real_close(*a, **k)

            sess.close = _close
            return sess

        db_manager.get_session = _tracked_get_session
        try:
            with caplog.at_level(logging.INFO, logger="models"):
                answer = db_manager._lookup_byo_mode(SEED_TENANT, session=_DeadConnectionSession())
        finally:
            db_manager.get_session = original_get_session

        assert answer == "local_allowed", (
            "a first-attempt fault that CLEARS on a retry is a transient, "
            "connection-scoped blip, not an unresolvable posture — denying here "
            "flips 29 of 52 prod tenants to a 402 that is cached for 300s and "
            f"not retried by requirements_management; got {answer!r}"
        )
        assert closed == [1], (
            "the retry must close the session it opened — nobody else can, so "
            f"skipping it leaks a connection per blip; close calls: {len(closed)}"
        )

        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "byo_mode lookup for" in r.getMessage()
        ]
        assert infos, (
            "a recovered lookup must leave evidence at INFO — this line is the "
            "operator's ONLY signal that connection-scoped faults are happening "
            f"on the auth hot path; got {caplog.records!r}"
        )
        assert SEED_TENANT in infos[0].getMessage(), (
            "the INFO must name the tenant, or the rate cannot be attributed; "
            f"got {infos[0].getMessage()!r}"
        )

    def test_retry_uses_and_closes_a_session_this_function_owns(self, db_manager, monkeypatch):
        """On the FAILURE path too: the retry's session is ours, and we close it.

        ``get_session()`` has no context manager here, so if the retry does not
        close in a ``finally`` a persistent fault leaks one connection per
        introspect — precisely when the pool is already under stress. The
        borrowed session must still be left completely untouched.
        """

        class _BorrowedSession:
            def query(self, *a, **k):
                raise RuntimeError("db down")

            def rollback(self):
                raise AssertionError("a borrowed session must not be rolled back by the callee")

            def close(self):
                raise AssertionError("a borrowed session must not be closed by the callee")

        class _RetrySession:
            def __init__(self):
                self.closed = 0

            def query(self, *a, **k):
                raise RuntimeError("db still down")

            def rollback(self):
                pass

            def close(self):
                self.closed += 1

        opened: list[_RetrySession] = []

        def _open():
            s = _RetrySession()
            opened.append(s)
            return s

        monkeypatch.setattr(db_manager, "get_session", _open)

        assert (
            db_manager._lookup_byo_mode("any-tenant", session=_BorrowedSession()) == "byo_required"
        )
        assert len(opened) == 1, (
            f"the retry must open exactly one session of its own; got {len(opened)}"
        )
        assert opened[0].closed == 1, (
            "the retry must close its own session in a `finally` — on the "
            f"failure path as well; close calls: {opened[0].closed}"
        )

    def test_a_borrowed_session_that_raised_is_not_reused(self, db_manager, monkeypatch):
        """Retrying on the session that just raised cannot work, ever.

        A session whose statement raised is in an ABORTED transaction: every
        further statement on it raises ``InFailedSqlTransaction``. And it may be
        the CALLER's session, which we may neither roll back nor close to clear
        that state. So a retry that reuses it is guaranteed to fail — it would
        turn the whole seam into an expensive no-op that always denies.
        """

        class _BorrowedSession:
            def __init__(self):
                self.queries = 0

            def query(self, *a, **k):
                self.queries += 1
                raise RuntimeError("db down")

            def rollback(self):
                raise AssertionError("a borrowed session must not be rolled back by the callee")

            def close(self):
                raise AssertionError("a borrowed session must not be closed by the callee")

        class _RetrySession:
            def query(self, *a, **k):
                raise RuntimeError("db still down")

            def rollback(self):
                pass

            def close(self):
                pass

        opened: list[_RetrySession] = []

        def _open():
            s = _RetrySession()
            opened.append(s)
            return s

        monkeypatch.setattr(db_manager, "get_session", _open)

        borrowed = _BorrowedSession()
        assert db_manager._lookup_byo_mode("any-tenant", session=borrowed) == "byo_required"
        assert borrowed.queries == 1, (
            "the retry must NOT re-run on the borrowed session that just raised "
            "— its transaction is aborted, so the second attempt could only ever "
            f"fail; it was queried {borrowed.queries}x"
        )
        assert opened and opened[0] is not borrowed, (
            "the retry must run on a session obtained from get_session(), not on "
            "the one it was handed"
        )

    def test_no_retry_when_the_first_attempt_succeeds(self, db_manager, monkeypatch):
        """The happy path must cost exactly what it cost before the retry existed.

        ``_lookup_byo_mode`` runs on every single token introspection against a
        postgres reached over pgbouncer on another node, so a speculative second
        session or a second SELECT here is a fleet-wide latency regression.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")

        original_get_session = db_manager.get_session
        opened: list[int] = []

        def _counting_get_session():
            opened.append(1)
            return original_get_session()

        monkeypatch.setattr(db_manager, "get_session", _counting_get_session)

        with _count_selects(db_manager.engine) as rec:
            assert db_manager._lookup_byo_mode(SEED_TENANT) == "local_allowed"

        assert len(opened) == 1, (
            "the success path must open ONE session — a retry on a healthy "
            f"lookup doubles pgbouncer checkouts on the auth hot path; got {len(opened)}"
        )
        assert rec.selects == 1, (
            "the success path must issue ONE SELECT (the tenant row); got "
            f"{rec.selects}:\n" + "\n".join(rec.statements)
        )


class TestPerAppByoMode:
    """Per-(tenant, product) posture override (tenant_app_byo_mode)."""

    def test_override_wins_over_tenant_wide(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="byo_required")
        result = db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        assert result is not None and result["byo_mode"] == "platform_only"
        # Per-product lookup returns the override...
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "platform_only"
        # ...while the tenant-wide value is untouched.
        assert db_manager._lookup_byo_mode(SEED_TENANT) == "byo_required"

    def test_no_override_falls_back_to_tenant_wide(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="local_allowed")
        # App exists but no override row → tenant-wide value applies.
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"

    def test_unknown_app_key_falls_back_to_tenant_wide(self, db_manager):
        _make_tenant(db_manager, byo_mode="local_allowed")
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key="no-such-app") == "local_allowed"

    def test_set_by_tenant_name(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="byo_required", name="acme-corp")
        result = db_manager.set_tenant_app_byo_mode("acme-corp", seed_app.key, "local_allowed")
        assert result is not None and result["tenant_name"] == "acme-corp"
        assert db_manager._lookup_byo_mode("acme-corp", app_key=seed_app.key) == "local_allowed"

    def test_upsert_updates_existing(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="byo_required")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "local_allowed")
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"

    def test_create_reports_prior_byo_mode_none(self, db_manager, seed_app):
        """No row before the write → ``prior_byo_mode`` is None, i.e. "there was none".

        This is what tells a failed provision to CLEAR the override rather than
        write a phantom row back.
        """
        _make_tenant(db_manager, byo_mode="byo_required")
        result = db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        assert result is not None
        assert result["prior_byo_mode"] is None

    def test_update_reports_the_value_it_overwrote(self, db_manager, seed_app):
        """The value at risk. Captured INSIDE the write's own session — a
        read-then-write from the caller would be a TOCTOU across two pooled
        connections, and this row is the tenant's live posture."""
        _make_tenant(db_manager, byo_mode="byo_required")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        result = db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "local_allowed")
        assert result is not None
        assert result["prior_byo_mode"] == "platform_only"
        assert result["byo_mode"] == "local_allowed"

    def test_prior_byo_mode_is_reported_by_the_http_route(
        self, auth_client_admin, db_manager, seed_app
    ):
        """``PUT /tenants/{t}/apps/{app}/byo-mode`` returns the DAL dict verbatim,
        so the new key is part of that route's published body (additive)."""
        _make_tenant(db_manager, byo_mode="byo_required")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        resp = auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "local_allowed"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["prior_byo_mode"] == "platform_only"

    def test_clear_reverts_to_tenant_wide(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="byo_required")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        assert db_manager.clear_tenant_app_byo_mode(SEED_TENANT, seed_app.key) is True
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "byo_required"

    def test_clear_nonexistent_returns_false(self, db_manager, seed_app):
        _make_tenant(db_manager, byo_mode="byo_required")
        assert db_manager.clear_tenant_app_byo_mode(SEED_TENANT, seed_app.key) is False

    def test_invalid_value_raises(self, db_manager, seed_app):
        _make_tenant(db_manager)
        with pytest.raises(ValueError, match="invalid byo_mode"):
            db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "bogus")

    def test_unknown_tenant_returns_none(self, db_manager, seed_app):
        assert (
            db_manager.set_tenant_app_byo_mode(
                "99999999-0000-7000-8000-000000000000", seed_app.key, "byo_required"
            )
            is None
        )

    def test_unknown_app_returns_none(self, db_manager):
        _make_tenant(db_manager)
        assert (
            db_manager.set_tenant_app_byo_mode(SEED_TENANT, "no-such-app", "byo_required") is None
        )
