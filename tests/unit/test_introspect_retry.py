"""``introspect_token``'s ONE bounded retry on a connect-scoped fault.

On 2026-08-11 a single introspect out of 595 died inside NullPool's connect
with ``server closed the connection unexpectedly`` — the traceback topped out
in ``Connection.__init__``, so no statement had run. ``/license/introspect`` is
called DIRECTLY by application pods (modality-gateway, w2v/youtube-upload-...),
not through license-authz, so none of them sit behind its stale-while-error
store: that 500 is the caller's whole answer.

Every fault here is injected through fake sessions rather than a real broken
connection, deliberately. ``tests/conftest.py`` does ``pytest.skip("PostgreSQL
not available")`` when it cannot reach the DB, so a test that needed a genuine
connection failure would SKIP — reporting green — exactly when it mattered.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

import psycopg
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

# The logger ``models.py`` writes through (``logging.getLogger(__name__)``).
MODELS_LOGGER = "models"


def _connect_fault() -> OperationalError:
    """The observed incident, reproduced in the shape SQLAlchemy delivers it.

    A CONNECT-time fault carries ``statement=None`` / ``params=None`` because
    no statement was ever sent — this is the wrapper SQLAlchemy builds inside
    ``Connection.__init__``.
    """
    return OperationalError(
        None,
        None,
        psycopg.OperationalError(
            'connection failed: connection to server at "10.43.159.32", port 6432 failed: '
            "server closed the connection unexpectedly"
        ),
    )


def _statement_timeout() -> OperationalError:
    """SQLSTATE 57014 — a SUBCLASS of ``psycopg.OperationalError``."""
    return OperationalError(
        "SELECT 1",
        {},
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
    )


class _FaultingSession:
    """A session whose first statement raises. Counts its own use and teardown."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.queries = 0
        self.closed = 0

    def query(self, *a, **k):
        self.queries += 1
        raise self._exc

    def rollback(self):
        pass

    def close(self):
        self.closed += 1


class _SessionLedger:
    """Hands out real sessions and records whether each one was CLOSED.

    Every test here that lets a real session through needs this, not just the
    ones nominally about cleanup. A session the code under test forgets to
    close holds its transaction open, and the damage does not surface as a
    failed assertion — it surfaces as the NEXT test's ``TRUNCATE`` blocking on
    the lock, i.e. as a hang. A hung run is grey, reads as suite flake, and (as
    a mutation pass here showed) can swallow the very test that was supposed to
    catch the leak by deadlocking before it runs. So the close is asserted at
    the first opportunity, and ``release()`` frees whatever leaked so one
    failure does not poison every test after it.
    """

    def __init__(self, real_get_session, monkeypatch):
        self._real_get_session = real_get_session
        self._monkeypatch = monkeypatch
        self._closers: list[Callable[[], None]] = []
        self.opened: list[object] = []
        self.closes = 0

    def open_real(self) -> Session:
        session = self._real_get_session()
        original_close = session.close

        def _counting_close():
            self.closes += 1
            original_close()

        self._monkeypatch.setattr(session, "close", _counting_close)
        self._closers.append(original_close)
        self.opened.append(session)
        return session

    def track_fake(self, session: object) -> object:
        self.opened.append(session)
        return session

    def release(self) -> None:
        for close in self._closers:
            with contextlib.suppress(Exception):
                close()


class TestIntrospectConnectRetry:
    """The retry seam itself: what it retries, how often, and on what."""

    def test_a_connect_fault_is_retried_on_a_fresh_session_and_succeeds(
        self, db_manager, seed_token, monkeypatch
    ):
        """The whole point: the incident becomes a 200 instead of a 500."""
        _token, raw = seed_token
        ledger = _SessionLedger(db_manager.get_session, monkeypatch)

        def _open():
            if not ledger.opened:
                return ledger.track_fake(_FaultingSession(_connect_fault()))
            return ledger.open_real()

        monkeypatch.setattr(db_manager, "get_session", _open)

        try:
            result = db_manager.introspect_token(raw)

            assert result is not None and result["active"] is True, result
            assert len(ledger.opened) == 2, (
                f"expected the failed attempt plus exactly one retry; "
                f"got {len(ledger.opened)} sessions"
            )
            assert ledger.closes == 1, (
                "the retry's session must be closed in a `finally` — an unclosed one "
                f"holds its transaction and its pgbouncer slot; closes: {ledger.closes}"
            )
        finally:
            ledger.release()

    def test_an_interface_error_is_retried_too(self, db_manager, seed_token, monkeypatch):
        """``InterfaceError`` is NOT an ``OperationalError`` subclass.

        ``sqlalchemy.exc.InterfaceError`` inherits ``DBAPIError`` directly,
        skipping ``DatabaseError``, so a predicate of ``OperationalError``
        alone silently misses the same physical fault arriving one layer up.
        """
        _token, raw = seed_token
        ledger = _SessionLedger(db_manager.get_session, monkeypatch)

        def _open():
            if not ledger.opened:
                return ledger.track_fake(
                    _FaultingSession(
                        InterfaceError(
                            None, None, psycopg.InterfaceError("connection already closed")
                        )
                    )
                )
            return ledger.open_real()

        monkeypatch.setattr(db_manager, "get_session", _open)

        try:
            assert db_manager.introspect_token(raw)["active"] is True
            assert len(ledger.opened) == 2, (
                f"InterfaceError must be retried; got {len(ledger.opened)} sessions"
            )
            assert ledger.closes == 1, (
                f"the retry's session must be closed; closes: {ledger.closes}"
            )
        finally:
            ledger.release()

    def test_a_deterministic_error_is_not_retried(self, db_manager, seed_token, monkeypatch):
        """A non-DB fault must propagate on the FIRST attempt.

        Re-running a deterministic bug cannot recover anything; it only doubles
        latency on the platform-wide auth hot path.
        """
        _token, raw = seed_token
        opened: list[object] = []

        def _open():
            session = _FaultingSession(RuntimeError("a real bug, not a blip"))
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with pytest.raises(RuntimeError, match="a real bug"):
            db_manager.introspect_token(raw)

        assert len(opened) == 1, (
            f"a deterministic error must NOT be retried; opened {len(opened)} sessions"
        )

    def test_a_statement_timeout_is_not_retried(self, db_manager, seed_token, monkeypatch):
        """SQLSTATE 57014 arrives wrapped in an ``OperationalError``.

        ``psycopg.errors.QueryCanceled`` SUBCLASSES ``psycopg.OperationalError``,
        so a naive ``except OperationalError`` swallows a statement timeout and
        retries it. The connection is fine and the server is merely slow, so a
        retry can only spend the budget twice and push the caller past its own
        deadline.
        """
        _token, raw = seed_token
        opened: list[object] = []

        def _open():
            session = _FaultingSession(_statement_timeout())
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with pytest.raises(OperationalError):
            db_manager.introspect_token(raw)

        assert len(opened) == 1, (
            "a statement timeout must NOT be retried even though QueryCanceled is an "
            f"OperationalError subclass; opened {len(opened)} sessions"
        )

    def test_the_session_that_raised_is_not_reused(self, db_manager, seed_token, monkeypatch):
        """Retrying on the session that just raised cannot work, ever.

        Its transaction is ABORTED, so a further statement raises 25P02 — which
        psycopg maps to ``InternalError``, NOT ``OperationalError``, so it would
        escape the retry predicate as a hard 500. And ``expire_on_commit=False``
        leaves a dirty ``last_used_at`` in the identity map, so a reused session
        would re-flush that UPDATE onto a dead connection.
        """
        _token, raw = seed_token
        opened: list[_FaultingSession] = []

        def _open():
            session = _FaultingSession(_connect_fault())
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with pytest.raises(OperationalError):
            db_manager.introspect_token(raw)

        assert len(opened) == 2, f"expected exactly two attempts; got {len(opened)}"
        assert opened[0] is not opened[1], "the retry must run on a session of its own"
        assert opened[0].queries == 1, (
            "the retry must NOT re-run on the session that just raised — its "
            f"transaction is aborted; it was queried {opened[0].queries}x"
        )

    def test_exactly_two_attempts_and_no_more(self, db_manager, seed_token, monkeypatch):
        """One retry, not a loop. The caller's introspect budget is 3.0s."""
        _token, raw = seed_token
        opened: list[_FaultingSession] = []

        def _open():
            session = _FaultingSession(_connect_fault())
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with pytest.raises(OperationalError):
            db_manager.introspect_token(raw)

        assert len(opened) == 2, (
            f"the retry is bounded at ONE re-dial; {len(opened)} sessions were opened"
        )

    def test_both_sessions_are_closed_on_the_failure_path(
        self, db_manager, seed_token, monkeypatch
    ):
        """Neither session may leak a pgbouncer client slot when both fail."""
        _token, raw = seed_token
        opened: list[_FaultingSession] = []

        def _open():
            session = _FaultingSession(_connect_fault())
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with pytest.raises(OperationalError):
            db_manager.introspect_token(raw)

        assert [s.closed for s in opened] == [1, 1], (
            "both the failed attempt and the retry must be closed in a `finally`; "
            f"close counts: {[s.closed for s in opened]}"
        )

    def test_a_close_fault_after_a_successful_read_does_not_trigger_a_retry(
        self, db_manager, seed_token, monkeypatch
    ):
        """A fault during CLEANUP must not be mistaken for a fault in the read.

        This is what goes wrong if the retry is wrapped around the whole
        ``try/finally`` instead of the body: the answer is already in hand, and
        re-running the work to recover from a failed ``close()`` is both
        pointless and a second pgbouncer checkout.

        ``closed`` is asserted alongside the session count, and it is the half
        that has to be here. Move the close out of the ``finally`` and the
        success path returns before ever reaching it, so the session LEAKS with
        its transaction open — which does not fail any assertion, it DEADLOCKS
        the next TRUNCATE in conftest teardown. A hang is a grey run, not a red
        one. Asserting the close makes that mutant fail loudly instead, and the
        ``finally`` below releases the leak so the deadlock never happens.
        """
        _token, raw = seed_token
        ledger = _SessionLedger(db_manager.get_session, monkeypatch)

        def _open():
            session = ledger.open_real()
            counting_close = session.close

            def _close_then_fault():
                counting_close()
                raise _connect_fault()

            monkeypatch.setattr(session, "close", _close_then_fault)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        try:
            assert db_manager.introspect_token(raw)["active"] is True
            assert len(ledger.opened) == 1, (
                "a close() fault after a successful read must not trigger a retry; "
                f"opened {len(ledger.opened)} sessions"
            )
            assert ledger.closes == 1, (
                "the session must be closed inside the `finally`, so the success path "
                f"cannot return past it and leak the transaction; closes: {ledger.closes}"
            )
        finally:
            ledger.release()

    def test_exhaustion_reraises_rather_than_reporting_the_token_invalid(
        self, db_manager, seed_token, monkeypatch
    ):
        """Fail LOUD, never fail open.

        Returning ``{"active": False}`` or ``None`` on an infrastructure fault
        would report a perfectly valid token as invalid — silent, fleet-wide
        de-authorization that looks like a credential problem. A 500 is correct.
        """
        _token, raw = seed_token

        monkeypatch.setattr(db_manager, "get_session", lambda: _FaultingSession(_connect_fault()))

        with pytest.raises(OperationalError):
            db_manager.introspect_token(raw)

    def test_the_happy_path_opens_exactly_one_session(self, db_manager, seed_token, monkeypatch):
        """The success path must cost exactly what it cost before this seam existed.

        A speculative second session, or a pre-ping probe, is a fleet-wide
        latency regression on the auth hot path — every call dials pgbouncer
        afresh under NullPool.

        The close is asserted for the same reason: under NullPool a session
        that is not closed is a pgbouncer client slot that is not returned, and
        a leak on the SUCCESS path leaks on every single call. It also fails
        silently — the leak shows up as a hung TRUNCATE two tests later, which
        reads as a flaky suite rather than as this defect.
        """
        _token, raw = seed_token
        ledger = _SessionLedger(db_manager.get_session, monkeypatch)
        monkeypatch.setattr(db_manager, "get_session", ledger.open_real)

        try:
            assert db_manager.introspect_token(raw)["active"] is True
            assert len(ledger.opened) == 1, (
                f"the success path must open ONE session; got {len(ledger.opened)}"
            )
            assert ledger.closes == 1, (
                "the success path must close its session — an unclosed session holds a "
                f"pgbouncer client slot on every call; closes: {ledger.closes}"
            )
        finally:
            ledger.release()


class TestIntrospectRetryLogging:
    """This service has no prometheus client, so the log line IS the signal."""

    def test_recovery_is_logged_at_info_and_nothing_louder(
        self, db_manager, seed_token, monkeypatch, caplog
    ):
        """A recovered blip is INFO — it is a rate to alert on, not an incident.

        The LEVEL is the whole point of this line, so it is asserted directly:
        a mutant that leaves the recovery at WARNING survives a test that only
        matches the event name.
        """
        _token, raw = seed_token
        real_get_session = db_manager.get_session
        opened: list[object] = []

        def _open():
            session = _FaultingSession(_connect_fault()) if not opened else real_get_session()
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with caplog.at_level(logging.INFO, logger=MODELS_LOGGER):
            assert db_manager.introspect_token(raw)["active"] is True

        recovered = [r for r in caplog.records if "introspect_retry_recovered" in r.getMessage()]
        assert len(recovered) == 1, (
            f"expected one recovery line; got {[r.getMessage() for r in caplog.records]}"
        )
        assert recovered[0].levelno == logging.INFO, (
            f"the recovery line must be INFO, not {recovered[0].levelname}"
        )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a recovered transient must not raise anything at WARNING or above: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= 30]}"
        )

    def test_exhaustion_is_logged_at_warning_with_both_causes(
        self, db_manager, seed_token, monkeypatch, caplog
    ):
        """Both failures must be recorded — the second one alone hides the pattern."""
        _token, raw = seed_token

        def _open():
            return _FaultingSession(_connect_fault())

        monkeypatch.setattr(db_manager, "get_session", _open)

        with caplog.at_level(logging.INFO, logger=MODELS_LOGGER):
            with pytest.raises(OperationalError):
                db_manager.introspect_token(raw)

        exhausted = [r for r in caplog.records if "introspect_retry_exhausted" in r.getMessage()]
        assert len(exhausted) == 1, (
            f"expected one exhaustion line; got {[r.getMessage() for r in caplog.records]}"
        )
        assert exhausted[0].levelno == logging.WARNING, (
            f"exhaustion must be WARNING, not {exhausted[0].levelname}"
        )
        assert not [r for r in caplog.records if "introspect_retry_recovered" in r.getMessage()], (
            "a call that never recovered must not emit the recovery line"
        )


class TestIntrospectRetryIsIdempotent:
    """A re-run must not double the one write on this path."""

    def test_a_commit_fault_does_not_double_the_last_used_at_write(
        self, db_manager, seed_token, monkeypatch
    ):
        """The retry replays the whole call, so the write must be safe to replay.

        ``last_used_at`` is a monotone last-writer-wins timestamp behind a 60s
        throttle: if the first attempt's COMMIT lands server-side but its ACK is
        lost, the retry re-reads a fresh timestamp, the throttle predicate is
        then False, and no second UPDATE is issued at all.

        THIS TEST WAS VACUOUS AS FIRST WRITTEN, and two independent audits
        caught it. It primed ``last_used_at`` first, so the measured call fell
        INSIDE the 60s throttle window — ``touched`` stayed False, the
        ``if touched: session.commit()`` never ran, the commit-throwing lambda
        was dead code, no fault was ever raised, no retry ever happened, and
        ``rec.verbs["update"]`` was 0, making ``<= 1`` trivially true. It
        asserted nothing about the property in its own name.

        The priming call is gone: a cold token writes ``last_used_at``, so the
        commit DOES run and the injected fault DOES fire. The assertions now
        pin all three things that make the replay safe — the fault was raised,
        a retry happened, and the write did not double.
        """
        from tests.unit.test_introspect_query_count import _count_statements

        _token, raw = seed_token

        real_get_session = db_manager.get_session
        opened: list[object] = []

        def _open():
            session = real_get_session()
            if not opened:
                # A REAL lost ACK: the commit LANDS server-side, and only the
                # acknowledgement is lost. Replacing commit with a bare raise
                # would be a different fault — the write would never happen, so
                # the retry would re-read `last_used_at is None`, `touched`
                # would be True again, and the count would stay at 1 because
                # attempt 1 wrote NOTHING rather than because the throttle
                # suppressed a replay. That version passes for the opposite
                # reason to the one this test exists to prove, and a regression
                # dropping the throttle predicate would not turn it red.
                real_commit = session.commit

                def _commit_then_lose_the_ack():
                    real_commit()
                    raise _connect_fault()

                monkeypatch.setattr(session, "commit", _commit_then_lose_the_ack)
            opened.append(session)
            return session

        monkeypatch.setattr(db_manager, "get_session", _open)

        with _count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)

        assert result["active"] is True
        assert len(opened) == 2, (
            "the injected commit fault must actually FIRE and be retried — if only "
            f"one session was opened the fault never happened; opened {len(opened)}"
        )
        assert rec.verbs["update"] == 1, (
            "the committed write must happen ONCE: the retry re-reads a fresh "
            "last_used_at, so the throttle suppresses the replay. Statements:\n"
            + "\n".join(rec.statements)
        )
