"""The boot seed must not be able to re-create a tenant whose id is a principal.

⛔ THE DEFECT, AND WHY IT RE-ARMED ON EVERY BOOT. ``ensure_tenant`` refuses to
register an id that is already a Keycloak principal (``subject.keycloak_sub`` or
``subject.id``) — a tenant is an ORGANISATION, a subject is an identity that
authenticates within one, and registering the second as the first makes the
authenticating identity the thing that PAYS. Nothing downstream can detect it:
``billing_cost_events`` has no FK to any tenant table and UUID version does not
separate the two populations.

``seed_platform_tenants`` did not go through ``ensure_tenant``. It issued a raw
``INSERT … ON CONFLICT DO NOTHING`` on its own connection, so the guard could not see
it from ANY position — and two of the ids it re-INSERTs on every boot violate the
invariant (``e148e8af-…`` is both a ``tenants`` row and a ``subject.keycloak_sub`` on
prod; the ``00000000-…0000`` sentinel likewise). The guard was bypassable by the one
code path that runs unconditionally on every restart.

⛔ WHAT THIS IS NOT. It is not a repair, and it must never become one. Re-keying an
existing row is a data migration with its own blast radius — 1,399 billing rows /
$86.02 in 24h hang off ``e148e8af-…`` — and is explicitly out of scope. An existing
row is left EXACTLY as it is. What is closed is the birth of a NEW one.

⛔ AND IT MUST NOT CRASH. This runs inside ``_run_db_bootstrap``, where anything that
is not a connect fault propagates and the pod REFUSES TO SERVE — and this service
backs ext_authz for the whole platform. Both known hits have been true for weeks, so a
fatal check would CrashLoop both replicas of the auth path over nothing new. A refusal
is a loud log line plus a counter.

Design notes — the ways a test like this normally fails to be a test:

* The refusal is proven by MEASURING THE ROW COUNT, not by reading the log. A log
  line can be emitted by a seed that inserted anyway.
* The fail-OPEN direction is exercised too (an unreadable ``subject`` table must not
  block a fresh-database rebuild), because a guard that fails closed here would break
  the disaster-recovery case these hardcoded ids exist for.
* It asserts the counter is keyed per-identity and not incremented per boot: the seed
  runs on every restart, and a tally would make the gauge a function of uptime.
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest

from models import PLATFORM_TENANTS, Subject
from services import tenant_subject_invariant as tsi


@pytest.fixture(autouse=True)
def _clean_refusal_register():
    """The register is PROCESS-lifetime, so it leaks between tests without this."""
    tsi.reset_seed_refusals()
    yield
    tsi.reset_seed_refusals()


def _seeded_ids() -> list[str]:
    return [tid for tid, _n, _b in PLATFORM_TENANTS]


def _make_subject_claiming(db_manager, tenant_id: str) -> Subject:
    """Write a ``subject`` row whose Keycloak ``sub`` IS a seeded tenant id.

    This is the real shape of the defect: the principal exists first (Keycloak minted
    it, provisioning recorded it) and the seed then tries to register the same UUID as
    an organisation.

    ⚠️ WRITTEN DIRECTLY, NOT THROUGH ``create_or_get_subject``, AND THAT CHANGED
    2026-09-23. This helper used to call that method. It cannot any more, because
    that method is now the guard on the SECOND door onto this same invariant: it
    refuses a NEW subject whose ``keycloak_sub`` is already a registered
    ``tenants.id``. In ``test_an_existing_row_is_left_alone`` the tenant row is
    created FIRST, so the old helper was refused — by the very guard this repo
    wanted — and the test failed in its SETUP.

    ⛔ The resolution is NOT to weaken that guard. These rows are fabrications of
    PRE-GUARD history: on prod they were born months before either door existed
    (``8382f0b3-…`` in 2026-01, ``019e97ad-…`` in 2026-06), and a test that needs a
    pre-existing violation must manufacture one the way history did, not through a
    door that now correctly refuses. Going through the DAL here would mean the
    fixture could only ever build states the guards permit — which is exactly the
    set of states that are NOT the defect.

    ⭐ That the old call is now refused is itself a proof that door 2 is shut; it is
    asserted deliberately, rather than left as a broken fixture, in
    ``tests/unit/test_subject_refuses_a_tenant_id.py``.
    """
    session = db_manager.get_session()
    try:
        subject = Subject(keycloak_sub=tenant_id, keycloak_realm="portal")
        session.add(subject)
        session.commit()
        session.refresh(subject)
        session.expunge(subject)
        return subject
    finally:
        session.close()


class TestTheSeedRefusesAPrincipalId:
    def test_the_row_is_not_created(self, db_manager) -> None:
        """⛔ THE CORE ASSERTION. Without the guard this id is INSERTed on every boot."""
        target = _seeded_ids()[0]
        _make_subject_claiming(db_manager, target)

        db_manager.seed_platform_tenants()

        session = db_manager.get_session()
        try:
            from models import Tenant

            row = session.query(Tenant).filter(Tenant.id == UUID(target)).first()
        finally:
            session.close()
        assert row is None, (
            f"the boot seed created tenant {target} even though that UUID is already a "
            "Keycloak principal id — the guard ensure_tenant enforces is still "
            "bypassable by the one path that runs on every restart"
        )

    def test_the_other_platform_tenants_are_still_seeded(self, db_manager) -> None:
        """⛔ A REFUSAL MUST NOT BE A FAILED SEED.

        This is the failure mode the implementation is shaped to avoid: run the probe
        inside the insert transaction and one refusal (or one faulting probe) aborts
        it, so every REMAINING row fails with ``InFailedSqlTransaction``. A guard that
        takes down the thing it protects is worse than the defect — a fresh database
        would come up missing every hardcoded identity the fleet names directly.
        """
        target = _seeded_ids()[0]
        _make_subject_claiming(db_manager, target)

        db_manager.seed_platform_tenants()

        session = db_manager.get_session()
        try:
            from models import Tenant

            present = {str(r[0]) for r in session.query(Tenant.id).all()}
        finally:
            session.close()
        expected = set(_seeded_ids()) - {target}
        assert expected <= present, (
            "refusing one entry stopped the others being seeded: "
            f"missing={sorted(expected - present)}"
        )

    def test_it_does_not_raise(self, db_manager) -> None:
        """Boot must survive it. A CrashLoop here takes the platform auth path down."""
        _make_subject_claiming(db_manager, _seeded_ids()[0])
        db_manager.seed_platform_tenants()  # must simply return

    def test_the_refusal_is_logged_at_error(
        self, db_manager, caplog: pytest.LogCaptureFixture
    ) -> None:
        target = _seeded_ids()[0]
        _make_subject_claiming(db_manager, target)
        with caplog.at_level(logging.ERROR, logger="models"):
            db_manager.seed_platform_tenants()
        joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert "REFUSING to seed tenant" in joined
        assert target in joined, "the refusal did not name the id, so nobody can action it"

    def test_the_refusal_is_counted_per_identity_not_per_boot(self, db_manager) -> None:
        """⛔ A TALLY WOULD MAKE THE GAUGE A FUNCTION OF UPTIME.

        The seed runs on every restart and, here, more than once per process. A refusal
        is a FACT about ``PLATFORM_TENANTS``, so three boots must report one refusal,
        not three.
        """
        target = _seeded_ids()[0]
        _make_subject_claiming(db_manager, target)
        db_manager.seed_platform_tenants()
        db_manager.seed_platform_tenants()
        db_manager.seed_platform_tenants()
        refusals = tsi.seed_refusals()
        assert len(refusals) == 1
        assert next(iter(refusals))[0] == target

    def test_a_clean_seed_records_nothing(self, db_manager) -> None:
        """The control. Without it, a register that ALWAYS held an entry would pass."""
        db_manager.seed_platform_tenants()
        assert tsi.seed_refusals() == {}

    def test_an_existing_row_is_left_alone(self, db_manager) -> None:
        """⛔ NEVER A REWRITE. The prod rows must survive untouched.

        This is the ``e148e8af-…`` case: the row already exists AND violates. The seed
        must neither re-key it, rename it, nor delete it — only decline to insert,
        which was already a no-op for an existing row.
        """
        from models import Tenant

        target = _seeded_ids()[0]
        session = db_manager.get_session()
        try:
            session.add(
                Tenant(
                    id=UUID(target),
                    name="pre-existing-violator",
                    byo_mode="platform_only",
                    owner_key="platform",
                )
            )
            session.commit()
        finally:
            session.close()
        _make_subject_claiming(db_manager, target)

        db_manager.seed_platform_tenants()

        session = db_manager.get_session()
        try:
            row = session.query(Tenant).filter(Tenant.id == UUID(target)).first()
        finally:
            session.close()
        assert row is not None
        assert row.name == "pre-existing-violator", "the seed rewrote an existing row"
        assert row.byo_mode == "platform_only"


class TestTheGuardFailsOpen:
    def test_an_unevaluable_guard_does_not_block_the_seed(self, db_manager, monkeypatch) -> None:
        """⛔ THE DR CASE. An unreadable ``subject`` table is not evidence of anything.

        These ids exist so a rebuilt database carries the identities the fleet
        hardcodes. If the guard failed CLOSED on its own fault, a probe error would
        leave a fresh database with no platform tenants at all — a strictly worse
        outcome than seeding unguarded, since ``ensure_tenant`` still refuses every
        new violation through the HTTP path.
        """

        def _explode(*_a, **_k):
            raise RuntimeError("subject table unreadable")

        monkeypatch.setattr(tsi, "subject_for_tenant_id", _explode, raising=True)
        inserted = db_manager.seed_platform_tenants()
        assert inserted == len(PLATFORM_TENANTS)
        assert tsi.seed_refusals() == {}, (
            "a guard that could not be evaluated recorded a refusal anyway — an "
            "unevaluable invariant is not a violation"
        )

    def test_the_fail_open_is_announced(
        self, db_manager, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently seeding unguarded is how this class of defect stays invisible."""

        def _explode(*_a, **_k):
            raise RuntimeError("subject table unreadable")

        monkeypatch.setattr(tsi, "subject_for_tenant_id", _explode, raising=True)
        with caplog.at_level(logging.WARNING, logger="models"):
            db_manager.seed_platform_tenants()
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "seeding UNGUARDED" in joined


class TestTheGuardIsTheSamePredicateAsEnsureTenant:
    """Two copies of "what a violation is" would drift; one import cannot."""

    def test_the_seed_imports_the_shared_predicate(self) -> None:
        import inspect

        import models

        source = inspect.getsource(models.DatabaseManager.seed_platform_tenants)
        assert "subject_for_tenant_id" in source, (
            "the seed no longer uses the shared write-time predicate; a second copy of "
            "the join is how the guard and the gauge come to disagree"
        )

    def test_ensure_tenant_still_refuses_directly(self, db_manager) -> None:
        """The other half of the guard must not have been weakened to make room."""
        from models import ClientSafeError

        target = "019e0f00-0000-7000-8000-0000000000c1"
        _make_subject_claiming(db_manager, target)
        with pytest.raises(ClientSafeError, match="already an existing"):
            db_manager.ensure_tenant(target, name="ensure-guard-still-live", owner_key="platform")
