"""Revoking a leaked token must not leave that same token LIVE on another licence.

The defect
----------
A raw token is treated everywhere as if it identified exactly ONE row. The schema
never says so. The only UNIQUE index is ``ux_token_license_hash (license_id,
token_hash)``; ``idx_token_hash`` is NON-unique. So the same hash can sit on
several licences at once — and in production it does, measured on the primary
2026-08-19::

    8a95ee68 -> procedure_management + procedures                       (2 licences)
    a9180e38 -> n8n + secrets                                           (2 licences)
    dfa1d6fa -> kaizen_core_actions + metering + kaizen_discover_actions (3 licences)

All 7 rows unrevoked, all perpetual, all on ``active`` licences, all one tenant.

Two consequences, both proven below:

1. **Incident response silently failed.** ``revoke_token_for_tenant`` selected
   ONE row with ``.first()``, flipped it, and returned ``"revoked"`` — the
   operator was told the leaked credential was dead. The twin on the other
   licence was untouched and still introspected ``active``. Re-running returned
   ``"already_revoked"`` (an idempotent *success*) whenever ``.first()`` landed
   back on the killed row, so the operator was told TWICE that a live credential
   was dead. ``revoke_tokens_for_license`` cannot help: it is scoped to one
   ``license_id`` and cannot reach a twin.
2. **Which entitlement the token carried was heap order.** No ``ORDER BY``
   anywhere, so ``.first()`` returned whatever PostgreSQL handed back — which
   changes after any UPDATE/VACUUM/restore and differs between replicas.
   ``_introspect_from_session`` UPDATEs ``last_used_at`` on the row it chose, so
   the ordering actively churns.

The RED proof, enumerated
-------------------------
Run against ``origin/main`` (c3dfab6): **10 failed, 2 passed**. Not all ten are
equal, and saying so matters more than the number::

  FAIL (behavioural)  test_it_revokes_both_rows_and_reports_two
  FAIL (behavioural)  test_the_surviving_twin_cannot_introspect_afterwards
  FAIL (behavioural)  test_a_second_revoke_finishes_off_a_twin_instead_of_claiming_success
  FAIL (behavioural)  test_the_route_says_how_many_records_shared_the_token
  FAIL (behavioural)  test_the_shortest_lived_row_wins_not_heap_order
  FAIL (behavioural)  test_a_revoked_twin_wins_so_resolution_fails_closed
  FAIL (behavioural)  test_a_foreign_tenants_row_on_the_same_hash_is_not_revoked
  FAIL (behavioural)  test_introspect_warns_with_the_count
  FAIL (contract)     test_an_already_dead_hash_is_still_idempotent
  FAIL (contract)     test_a_hash_only_another_tenant_holds_is_still_not_found
  PASS                test_repeated_lookups_resolve_to_the_same_row (see its docstring)
  PASS                test_a_single_row_does_not_warn (anti-vacuity guard)

The two marked *contract* fail on main only because the return value became
``(outcome, count)``; the behaviour they assert was already correct. They are
regression guards, not proofs of the defect.

What is deliberately NOT changed
--------------------------------
No UNIQUE constraint on ``token_hash`` alone. That is the durable fix, but it is
a destructive migration against 7 live production rows and a separate decision.
And the tenant boundary: a row on the same hash in ANOTHER tenant must still be
left alone, pinned by ``TestTheTenantBoundaryStillHolds``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from models import Token

TENANT_ID = "01000000-0000-7000-8000-000000000001"


def _plant(db_manager, license_id, raw_token: str, *, expires_at=None, revoked=False) -> UUID:
    """Put a row carrying ``raw_token``'s hash onto ``license_id``.

    Written through the ORM rather than ``create_token`` because
    ``create_token`` mints its own random raw value — the whole point here is
    two licences carrying the SAME one, which the schema permits.
    """
    session = db_manager.get_session()
    try:
        token = Token(
            license_id=UUID(str(license_id)),
            token_hash=Token.hash_token(raw_token),
            token_type="api",
            expires_at=expires_at,
            revoked=revoked,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        session.add(token)
        session.commit()
        session.refresh(token)
        return UUID(str(token.id))
    finally:
        session.close()


def _rows(db_manager, raw_token: str) -> list[Token]:
    session = db_manager.get_session()
    try:
        return (
            session.query(Token)
            .filter(Token.token_hash == Token.hash_token(raw_token))
            .order_by(Token.created_at.asc())
            .all()
        )
    finally:
        session.close()


@pytest.fixture
def duplicated_token(db_manager, seed_license, seed_second_license) -> str:
    """ONE raw token on TWO active licences of the SAME tenant — the prod shape.

    Both perpetual and both unrevoked, exactly as the 7 measured rows are.
    """
    raw = "duplicated-across-two-licences"
    _plant(db_manager, seed_license.id, raw)
    _plant(db_manager, seed_second_license.id, raw)
    assert len(_rows(db_manager, raw)) == 2, "the fixture did not create the duplicate"
    return raw


class TestRevocationReachesEveryRowSharingTheHash:
    def test_it_revokes_both_rows_and_reports_two(
        self, db_manager, duplicated_token, seed_license, seed_second_license
    ):
        """RED on main: ``.first()`` kills ONE row and answers ``"revoked"``."""
        outcome, count = db_manager.revoke_token_for_tenant(duplicated_token, TENANT_ID)

        assert outcome == "revoked"
        assert count == 2, (
            f"revoke reported {count} row(s); the leaked token is on 2 licences, so "
            f"the operator was told the credential was dead while a twin survived"
        )
        assert [bool(row.revoked) for row in _rows(db_manager, duplicated_token)] == [True, True]

    def test_the_surviving_twin_cannot_introspect_afterwards(self, db_manager, duplicated_token):
        """The consequence the count is a proxy for: after the operator's revoke,
        the leaked credential must not authenticate. On main the untouched twin
        still answered ``active`` whenever resolution landed on it.
        """
        db_manager.revoke_token_for_tenant(duplicated_token, TENANT_ID)

        result = db_manager.introspect_token(duplicated_token)

        assert result["active"] is False, (
            "a revoked leaked token still introspects as active — the twin on the "
            "other licence survived the revoke"
        )

    def test_a_second_revoke_finishes_off_a_twin_instead_of_claiming_success(
        self, db_manager, seed_license, seed_second_license
    ):
        """The worst arm: a partially-revoked hash reported ``already_revoked``.

        Seeded as the pre-fix code leaves the estate — one row killed, its twin
        live. Answering "already revoked" there tells the operator a SECOND time
        that a live credential is dead.
        """
        raw = "half-revoked-leak"
        _plant(db_manager, seed_license.id, raw, revoked=True)
        _plant(db_manager, seed_second_license.id, raw)

        outcome, count = db_manager.revoke_token_for_tenant(raw, TENANT_ID)

        assert (outcome, count) == ("revoked", 1), (
            "a hash with one live row left answered as if everything was already dead"
        )
        assert all(bool(row.revoked) for row in _rows(db_manager, raw))

    def test_an_already_dead_hash_is_still_idempotent(
        self, db_manager, seed_license, seed_second_license
    ):
        """The other side of the same contract — nothing left to do is a success
        with a count of ZERO, and the audit timestamps are not rewritten."""
        raw = "entirely-dead-leak"
        _plant(db_manager, seed_license.id, raw, revoked=True)
        _plant(db_manager, seed_second_license.id, raw, revoked=True)
        stamps = [row.revoked_at for row in _rows(db_manager, raw)]

        assert db_manager.revoke_token_for_tenant(raw, TENANT_ID) == ("already_revoked", 0)
        assert [row.revoked_at for row in _rows(db_manager, raw)] == stamps

    def test_the_route_says_how_many_records_shared_the_token(
        self, auth_client_admin, db_manager, duplicated_token
    ):
        """The operator-visible half: an incident responder must be able to SEE
        that the credential was duplicated, not just that "it worked"."""
        response = auth_client_admin.post("/license/revoke", json={"token": duplicated_token})

        body = response.json()
        assert body["revoked"] is True
        assert "2" in body["message"], body["message"]


class TestResolutionIsDeterministic:
    def test_repeated_lookups_resolve_to_the_same_row(self, db_manager, duplicated_token):
        """⚠️ HONEST LABEL: this one can PASS on main. Heap order is stable while
        nothing rewrites the tuples, so a tight loop may agree with itself by
        accident. It is the literal invariant the caller depends on, so it is
        pinned here as a regression guard; the RED proof of the ordering defect is
        the next test, which pins WHICH row wins.
        """
        picked = {
            str(db_manager.get_token_by_hash(Token.hash_token(duplicated_token)).license_id)
            for _ in range(5)
        }

        assert len(picked) == 1, f"the same raw token resolved to {len(picked)} licences: {picked}"

    def test_the_shortest_lived_row_wins_not_heap_order(
        self, db_manager, seed_license, seed_second_license
    ):
        """RED on main: with no ``ORDER BY``, the row inserted FIRST comes back.

        Planted so heap order and TOKEN_HASH_PICK_ORDER disagree: the perpetual
        row goes in first (heap order would pick it), the bounded row second.
        The pick order prefers the LEAST capability — ``expires_at ASC NULLS
        LAST`` — so the bounded row must win.
        """
        raw = "ordering-disagrees-with-heap"
        perpetual = _plant(db_manager, seed_license.id, raw, expires_at=None)
        bounded = _plant(
            db_manager,
            seed_second_license.id,
            raw,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

        chosen = db_manager.get_token_by_hash(Token.hash_token(raw))

        assert UUID(str(chosen.id)) == bounded, (
            f"resolution picked {chosen.id}; expected the shortest-lived row "
            f"{bounded}, not the perpetual one {perpetual} that heap order returns"
        )

    def test_a_revoked_twin_wins_so_resolution_fails_closed(
        self, db_manager, seed_license, seed_second_license
    ):
        """The security consequence of clause 1 (``revoked DESC``).

        Planted so heap order would return the LIVE row first. Once any replica
        of a leaked hash has been revoked, introspection must resolve to it and
        answer inactive — a partially-revoked leak must not authenticate through
        the surviving twin.
        """
        raw = "one-twin-already-killed"
        _plant(db_manager, seed_license.id, raw)
        _plant(db_manager, seed_second_license.id, raw, revoked=True)

        assert db_manager.introspect_token(raw)["active"] is False, (
            "a hash with a revoked replica still authenticated through its live "
            "twin — resolution is not fail-closed"
        )


class TestTheTenantBoundaryStillHolds:
    def test_a_foreign_tenants_row_on_the_same_hash_is_not_revoked(
        self, db_manager, seed_license, seed_other_tenant_license
    ):
        """Revoking must reach every row THIS tenant owns and stop there.

        Nothing prevents two tenants from holding the same hash — it is the same
        missing constraint — and a revoke that crossed the boundary would let any
        ``license-manager`` kill another tenant's credential by presenting a
        value they already hold.
        """
        raw = "shared-across-two-tenants"
        _plant(db_manager, seed_license.id, raw)
        foreign_row_id = _plant(db_manager, seed_other_tenant_license.id, raw)

        outcome, count = db_manager.revoke_token_for_tenant(raw, TENANT_ID)

        assert (outcome, count) == ("revoked", 1)
        foreign = [row for row in _rows(db_manager, raw) if UUID(str(row.id)) == foreign_row_id]
        assert len(foreign) == 1
        assert bool(foreign[0].revoked) is False, (
            "the revoke crossed the tenant boundary and killed another tenant's token"
        )

    def test_a_hash_only_another_tenant_holds_is_still_not_found(
        self, db_manager, seed_other_tenant_license
    ):
        """Non-disclosure is unchanged: fetching the foreign rows (so the warning
        can report true multiplicity) must not turn this endpoint into an oracle.
        """
        raw = "only-the-other-tenant-holds-this"
        _plant(db_manager, seed_other_tenant_license.id, raw)

        assert db_manager.revoke_token_for_tenant(raw, TENANT_ID) == ("not_found", 0)
        assert bool(_rows(db_manager, raw)[0].revoked) is False


class TestTheMultiplicityIsVisible:
    def test_introspect_warns_with_the_count(self, db_manager, duplicated_token, caplog):
        """Nothing anywhere said this was happening — the 7 production rows were
        found only by querying the primary by hand."""
        with caplog.at_level(logging.WARNING, logger="models"):
            db_manager.introspect_token(duplicated_token)

        warnings = [r for r in caplog.records if "token_hash resolves to" in r.getMessage()]
        assert warnings, "token-hash multiplicity was resolved silently"
        assert "2 token rows" in warnings[0].getMessage(), warnings[0].getMessage()

    def test_a_single_row_does_not_warn(self, db_manager, seed_token, caplog):
        """Anti-vacuity: the warning must be about multiplicity, not about every
        introspect."""
        _token, raw = seed_token

        with caplog.at_level(logging.WARNING, logger="models"):
            db_manager.introspect_token(raw)

        assert not [r for r in caplog.records if "token_hash resolves to" in r.getMessage()]
