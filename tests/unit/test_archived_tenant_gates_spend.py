"""An ARCHIVED tenant's licence must be refused at the introspection boundary.

⭐ WHAT WAS ACTUALLY WRONG. `tenants.archived_at` gated PROVISIONING only — the
mint path (main.py) refuses to create a licence for an archived tenant, and
`list_tenants` filters them out. Nothing consulted it on the path that AUTHORIZES
USE. `/license/introspect` is what ext_authz and every on-behalf-of caller ask
before doing chargeable work, and it checked the token, the licence, and the
tenant's BYO posture — never whether the tenant had been turned off. So archiving
a tenant that held a live licence removed it from the console and changed nothing
about what it could spend.

This is the enforcement half of gitops #3795 (b5d1a0b9f), which shipped the
DETECTOR (`TenantSpendOnArchivedRegistryRow`, firing now) and filed the missing
authorizer as licenses#197.

⛔ THESE TESTS ARE NOT EVIDENCE THAT THE ALERT WILL CLEAR. It will not, and
anyone shipping this must know that. Re-measured on the prod primary 2026-09-11
with the emitter's own predicate (billing_cost_events, 7d, grouped by tenant_id):

  * Exactly ONE archived tenant is spending — `rmt-e2e`, 405 events / $1.6018 —
    and ALL 405 rows carry **license_id = NULL**, posted by `inference-router`
    and `mlx-ingestion-backend` on a path that never asks this service anything.
  * All 19 licences of all 17 archived tenants are `status='revoked'`, so every
    one ALREADY answered `license_inactive` and cannot reach the new gate.

The alert's population (archived + billing rows) and this gate's population
(archived + presents a licence) are DISJOINT today, so enforcement blast radius
is ZERO refusals. Closing the alert's population needs a resolved-licence
requirement at the cost-event writer, outside this repo.

The gate is still correct and still needed: `set_tenant_archived` revokes
nothing, and those 19 revocations were the nightly reaper on unrelated dates
(2026-04-26 → 2026-09-06), NOT a consequence of the 2026-09-03 archive. The next
tenant archived while holding a live licence is the one this protects. These
tests construct exactly that tenant, because production does not currently have
one.

⚠️ There is deliberately NO metric assertion here. #3795 refused per-tenant
Prometheus labels on purpose ("would put customer identity in Prometheus and
would grow unbounded"); the refusal is observable as a greppable WARNING log
line instead. Do not add a counter to make this "measurable" without reading
that decision first.
"""

from __future__ import annotations

from uuid import UUID

from models import Tenant
from tests.support.sql_counter import count_statements as _count_statements

SEED_TENANT = "01000000-0000-7000-8000-000000000001"  # matches the seed_license fixture


def _make_tenant(db_manager, *, name="archive-gate-test", tenant_id=SEED_TENANT):
    """Register the seed licence's tenant so there is a row to archive."""
    session = db_manager.get_session()
    try:
        # merge, not add: conftest's autouse _register_seed_tenants already
        # created this id to satisfy the license.tenant_id FK.
        session.merge(
            Tenant(id=UUID(tenant_id), name=name, byo_mode="local_allowed", owner_key="platform")
        )
        session.commit()
    finally:
        session.close()


class TestArchivedTenantIsRefused:
    def test_archived_tenant_with_a_genuinely_active_licence_is_refused(
        self, db_manager, seed_token
    ):
        """The defect, stated as a test.

        ⭐ THE LICENCE HERE IS GENUINELY ACTIVE — `seed_license` is
        `status='active'`, started yesterday, expires in 30 days — so this cannot
        pass for the wrong reason. Before the gate this introspect returned
        `active: True` with a full entitlement payload; the ONLY thing that
        changed is the tenant's `archived_at`.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token

        # Control FIRST: prove the licence authorizes while the tenant is live.
        # Without this the assertion below is satisfied by any broken fixture.
        before = db_manager.introspect_token(raw)
        assert before["active"] is True, (
            "precondition failed: the licence must authorize BEFORE the archive, "
            f"otherwise the refusal below proves nothing (got {before!r})"
        )

        archived = db_manager.set_tenant_archived(SEED_TENANT, True)
        assert archived is not None and archived.archived_at is not None

        after = db_manager.introspect_token(raw)
        assert after["active"] is False, (
            "an archived tenant's licence still authorized — archiving is not an "
            f"off-switch (got {after!r})"
        )
        assert after["reason"] == "tenant_archived", (
            "the refusal must be discriminable from license_inactive/token_invalid, "
            f"or an operator cannot tell WHY spend stopped (got {after['reason']!r})"
        )

    def test_refusal_carries_no_entitlement_payload(self, db_manager, seed_token):
        """A refusal must not leak the tenant/app/plan it just refused.

        ext_authz injects `tenant_id` downstream as `X-Tenant-Id`. A body that
        said `active: False` but still carried tenant/app/plan would let a
        careless consumer that reads the fields before checking `active` keep
        working — the exact shape that makes a deny cosmetic.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token
        db_manager.set_tenant_archived(SEED_TENANT, True)

        result = db_manager.introspect_token(raw)
        assert result["active"] is False
        for leaked in ("tenant_id", "license_id", "subject_id", "app", "plan", "byo_mode"):
            assert leaked not in result, (
                f"the refusal payload still carries {leaked!r} — a consumer reading "
                "fields before checking `active` would sail straight through"
            )

    def test_unarchiving_restores_the_licence(self, db_manager, seed_token):
        """The gate keys on `archived_at` and nothing else.

        Without this, a refusal that was really caused by something else the
        archive call happened to touch would look like a working gate. Archiving
        must be reversible in its effect, because it is reversible in the data.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token

        db_manager.set_tenant_archived(SEED_TENANT, True)
        assert db_manager.introspect_token(raw)["active"] is False

        db_manager.set_tenant_archived(SEED_TENANT, False)
        restored = db_manager.introspect_token(raw)
        assert restored["active"] is True, (
            "un-archiving did not restore the licence, so the refusal was not "
            f"caused by archived_at (got {restored!r})"
        )
        assert restored["byo_mode"] == "local_allowed", (
            "the byo posture must survive the archived gate sharing its row read"
        )

    def test_a_live_tenant_is_unaffected(self, db_manager, seed_token):
        """The negative control: 59 of 76 prod tenants are NOT archived."""
        _make_tenant(db_manager)
        _token, raw = seed_token
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result.get("reason") is None
        assert result["byo_mode"] == "local_allowed"


class TestTheFailOpenDirection:
    """⛔ THE FAIL DIRECTIONS ARE OPPOSITE, AND THIS CLASS IS THE ONLY THING
    HOLDING THAT IN PLACE.

    `_UNRESOLVED_TENANT_POSTURE` fabricates a posture when the tenant row cannot
    be resolved. `byo_mode` fails CLOSED (withholds a privilege); `archived`
    fails OPEN (does not refuse). Flip that one constant — a refactor "making the
    two fields consistent" with the byo fail-closed precedent immediately above
    it is the obvious way — and a pgbouncer blip on the platform-wide auth hot
    path makes every affected `/license/introspect` answer
    `active:false, reason:"tenant_archived"`. ext_authz then de-authorizes the
    fleet on an infrastructure wobble. That is the estate-wide outage
    `introspect_retry_exhausted` already refuses to cause by re-raising rather
    than fabricating "token invalid".

    ⚠️ THIS CLASS REPLACES A TEST THAT WAS VACUOUS, AND THE REASON IS WORTH
    KEEPING. It asserted this property through `introspect_token` after skipping
    its `_make_tenant` helper, with the comment "deliberately no row". There IS a
    row: `tests/conftest.py::_truncate_all` INSERTs `seed-tenant-primary`
    (byo_mode `byo_required`, archived_at NULL) before every test. So `active is
    True` passed because the tenant genuinely was not archived, and `byo_mode ==
    "byo_required"` passed because that is the literal seeded column value — not
    a fabrication. NEITHER branch was ever entered. Two mutants proved it:
    `_UNRESOLVED_TENANT_POSTURE` → `archived=True` SURVIVED, and replacing the
    no-row branch body with `raise AssertionError` SURVIVED — no test in the
    repo executed it.

    ⛔ So do not re-express these through `introspect_token` alone. The licence
    FK is `ondelete="RESTRICT"` (models.License.tenant_id), so a licence's tenant
    row CANNOT be deleted while the licence exists — the no-row branch is simply
    not reachable end-to-end from a seeded licence, and any test that claims to
    reach it that way is claiming something the schema forbids.
    """

    def test_an_unresolved_tenant_row_does_not_refuse(self, db_manager):
        """The no-row branch, entered for real.

        `"no-such-tenant"` is not UUID-shaped, so the resolver falls to the
        `Tenant.name` lookup, finds nothing, and returns the fabricated posture.
        Nothing is seeded under that name, so this genuinely enters the branch
        the previous test only claimed to.
        """
        posture = db_manager._lookup_tenant_posture("no-such-tenant")
        assert posture.archived is False, (
            "an unresolved tenant row was reported as ARCHIVED — a tenant that "
            "does not exist cannot have been archived, and treating 'no evidence' "
            "as a kill de-authorizes callers on a lookup miss"
        )
        assert posture.byo_mode == "byo_required", (
            "byo must still fail CLOSED on the same branch; only archived fails open"
        )
        assert posture.name is None

    def test_a_uuid_shaped_tenant_with_no_row_does_not_refuse(self, db_manager):
        """The same branch by the OTHER route into it.

        A UUID-shaped id resolves through `Tenant.id`; a name-shaped one through
        `Tenant.name`. Both miss into the same fabricated posture, and a future
        edit could plausibly change one path and not the other.
        """
        posture = db_manager._lookup_tenant_posture("019f0000-0000-7000-8000-00000000dead")
        assert posture.archived is False
        assert posture.byo_mode == "byo_required"

    def test_a_db_fault_does_not_refuse(self, db_manager, monkeypatch):
        """⭐ THE BRANCH THAT ACTUALLY THREATENS THE FLEET: RETRY EXHAUSTION.

        The no-row case is a lookup miss; THIS is the pgbouncer blip. BOTH
        attempts must fail — the first on the caller's session and the bounded
        retry on a fresh one — so `_lookup_tenant_posture` exhausts its retry and
        fabricates the posture. `archived` must be False there: an unresolvable
        row is evidence of nothing, and evidence of nothing must never be spent
        as a kill.

        ⚠️ FAULTING ONLY THE HANDED-IN SESSION IS NOT ENOUGH, and this test
        originally made that mistake. The retry opens a session of its OWN via
        `get_session()`, which is a live test-DB connection; the lookup then
        succeeds against it and returns via the NO-ROW branch instead. The
        assertion still passed, so the bug was invisible — it was caught only by
        mutating the no-row branch and watching this test fail with it. Patching
        the inner read is what makes both attempts fault.
        """
        calls = {"n": 0}

        def _always_faults(session, tenant_id, app_key):
            calls["n"] += 1
            raise RuntimeError("server closed the connection unexpectedly")

        monkeypatch.setattr(db_manager, "_tenant_posture_from_session", _always_faults)

        class _DeadSession:
            """A borrowed session; the callee may neither roll it back nor close it."""

            def rollback(self):
                raise AssertionError("a borrowed session must not be rolled back by the callee")

            def close(self):
                raise AssertionError("a borrowed session must not be closed by the callee")

        posture = db_manager._lookup_tenant_posture("any-tenant", session=_DeadSession())
        assert calls["n"] >= 2, (
            f"retry exhaustion was not reached — inner read ran {calls['n']}x, so this "
            "asserted the no-row branch, not the fault branch"
        )
        assert posture.archived is False, (
            "a DB fault on the auth hot path reported the tenant as ARCHIVED — "
            "every affected introspect would answer active:false and ext_authz "
            "would de-authorize the fleet on an infrastructure wobble"
        )
        assert posture.byo_mode == "byo_required", (
            "byo must still fail CLOSED on a fault; only archived fails open"
        )

    def test_introspect_still_authorizes_when_the_tenant_read_faults(
        self, db_manager, seed_token, monkeypatch
    ):
        """The same fault, end-to-end through the real hot path.

        The three tests above call the resolver directly. This one drives
        `introspect_token` with the REAL retry machinery and the REAL constant,
        faulting only the innermost tenant read — which is exactly the shape a
        pgbouncer blip takes. A licence whose tenant read failed must still
        authorize.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token

        calls = {"n": 0}

        def _faulting_read(session, tenant_id, app_key):
            calls["n"] += 1
            raise RuntimeError("server closed the connection unexpectedly")

        # Patch the innermost read, NOT _lookup_tenant_posture — patching the
        # latter would bypass the retry and the fabricated constant, which are
        # the two things under test.
        monkeypatch.setattr(db_manager, "_tenant_posture_from_session", _faulting_read)

        result = db_manager.introspect_token(raw)
        assert calls["n"] >= 2, (
            "the bounded retry did not run, so this exercised the wrong path "
            f"(inner read called {calls['n']}x)"
        )
        assert result["active"] is True, (
            "a tenant-read fault refused the licence — this is the fleet-wide "
            f"de-authorization the fail-open direction exists to prevent (got {result!r})"
        )
        assert result.get("reason") is None
        assert result["byo_mode"] == "byo_required"


class TestTheGateCostsNoExtraQuery:
    def test_introspect_still_issues_exactly_two_selects(self, db_manager, seed_token):
        """The archived flag rides the tenant row that was already being read.

        `/license/introspect` runs against a postgres reached over pgbouncer on
        another node, so a third SELECT here is a third cross-node round-trip on
        every ext_authz call in the fleet. This duplicates the pin in
        test_introspect_query_count.py deliberately: that file guards the byo
        lookup, and a future edit resolving `archived_at` with its own query
        would be a change to THIS feature, and should fail in THIS file.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token
        db_manager.introspect_token(raw)  # prime last_used_at out of the way

        with _count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)

        assert result["active"] is True
        assert rec.verbs["select"] == 2, (
            "the archived gate added a SELECT to the auth hot path; it must ride "
            f"the byo tenant read, got {rec.verbs['select']}:\n" + "\n".join(rec.statements)
        )

    def test_the_refusal_path_issues_no_extra_query_either(self, db_manager, seed_token):
        """A refused introspect must not be more expensive than an allowed one.

        Otherwise a tenant archived while a chatty client keeps polling (~288
        introspects/day for a 5-minute poll) turns an off-switch into extra load.
        """
        _make_tenant(db_manager)
        _token, raw = seed_token
        db_manager.introspect_token(raw)
        db_manager.set_tenant_archived(SEED_TENANT, True)

        with _count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)

        assert result["active"] is False
        assert rec.verbs["select"] == 2, (
            f"the refusal path issued {rec.verbs['select']} SELECTs:\n" + "\n".join(rec.statements)
        )
