"""A tenant id must be neither a `subject.keycloak_sub` nor a `subject.id`.

WHAT WAS WRONG, MEASURED ON PROD 2026-09-10.
`e148e8af-3751-49bf-a0fa-56ab46d31ddc` is a `tenants` row named
`portal-render-e2e` (kind='internal', cost_centre='ci') AND the `keycloak_sub`
of subject `8382f0b3-…` in realm `portal`. It drew 1,399 billing rows / $86.02
in 24h; the Keycloak user was deleted 2026-08-27 and the billing did not stop,
because it is sustained by the registry row plus 5 licences. Two more rows match
the licences-DB predicate: `1d0090ac-…` `book-fulfillment` (kind='customer') and
the deliberate `00000000-…0000` `default-anon` sentinel.

⚠️ THE UNION IS FOUR, NOT THREE. `e4f70a83-…` `nonagentic-gtm` is a LIVE Keycloak
principal registered as a tenant that was NEVER written to `subject`, so no
licences-DB predicate can see it — while a Keycloak-only check would miss
`portal-render-e2e`, whose principal was deleted. This module implements the
licences-DB door only and `TestTheBlindSpotIsDeclared` is what stops that being
forgotten.

⚠️ "PRINCIPAL", NOT "PERSON": `book-fulfillment` and `nonagentic-gtm` are
`secure-apps` SERVICE ACCOUNTS. Only `portal-render-e2e`'s subject is a human.

⚠️ NONE OF THESE TESTS IS "does it emit a number". Each pins a decision a future
edit could reverse with nothing going red:

  1. THE CAST. `subject.keycloak_sub` is a STRING and `tenants.id` is `uuid`, so
     the naive predicate does not return the wrong answer — it does not compile
     (`ERROR: operator does not exist: text = uuid`). These tests therefore run
     against a REAL PostgreSQL rather than a scripted fake session: a fake would
     have proved the row→series mapping while the SQL itself was uncompilable.
  2. THE SENTINEL SPLIT. `default-anon` is deliberate and permanent. If it
     shared a series with real violations the gauge could never reach 0, an
     alert on it would fire forever and be silenced inside a week. It must land
     in `class="sentinel"` and be ABSENT from `class="violation"` — while still
     MATCHING the predicate, because exempting it from the predicate would hide
     the day a fourth "deliberate" exception is added.
  3. THE TWO MATCH CLASSES. A Keycloak `sub` registered as a tenant and this
     registry's own `subject.id` registered as a tenant have different causes
     and different fixes. Collapsing them into one number hides which moved.
  4. THE REAL-SHAPED NEGATIVE. UUID VERSION IS NOT A DISCRIMINATOR: the genuine
     tenants `george-dekker`, `merqi` and `nonagentic-gtm` are v4, exactly like
     a Keycloak sub. A v4 tenant that is not a subject must NOT be flagged, or
     the instrument would condemn a third of the real estate.
  5. EXISTING VIOLATIONS ARE REPORTED, NOT FATAL. Three rows violate today and
     `seed_platform_tenants()` re-INSERTs two of them on EVERY boot. Neither the
     boot report nor the migration may raise on them.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from models import ClientSafeError, Subject
from services.tenant_subject_invariant import (
    CLASS_RETIRED_PRINCIPAL,
    CLASS_SENTINEL,
    CLASS_VIOLATION,
    KNOWN_KEYCLOAK_ONLY_BLIND_SPOTS,
    MATCH_KEYCLOAK_SUB,
    MATCH_SUBJECT_ID,
    RETIRED_PRINCIPAL_TENANT_IDS,
    SENTINEL_TENANT_IDS,
    SOURCE_KEYCLOAK_PRINCIPAL,
    SOURCE_LICENCES_SUBJECT,
    TenantSubjectCollector,
    collect_snapshot,
    report_at_boot,
    subject_for_tenant_id,
)

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "035_tenant_id_must_not_be_a_subject.sql"
DOWN = REPO / "migrations" / "downgrades" / "035_tenant_id_must_not_be_a_subject.sql"

SENTINEL_ID = "00000000-0000-0000-0000-000000000000"

# An ARBITRARY colliding id: a v4 uuid that is a `subject.keycloak_sub` AND a
# `tenants.id`, and is in NO register. The shape is v4, exactly like a Keycloak sub.
PROD_VIOLATOR = "7c1e3f52-9a4b-4d0e-8f61-2b5a9c7d1e03"

# A SYNTHETIC id the mechanism tests register for themselves (the `retired`
# fixture). The register is EMPTY in the shipped code: its only entry ever, the
# portal-render-e2e id below, was removed when migration 046 re-keyed that tenant.
RETIRED_PRINCIPAL_ID = "5d2f8a61-3c7e-4b19-a0d4-6e9f1b2c3a47"

# portal-render-e2e. USER sense: the `keycloak_sub` of subject `8382f0b3-…`, and
# until migration 046 ALSO the tenant's id. TENANT sense since 046: the uuid7.
PORTAL_RENDER_E2E_PRINCIPAL_SUB = "e148e8af-3751-49bf-a0fa-56ab46d31ddc"
PORTAL_RENDER_E2E_TENANT_ID = "01a1113b-674f-72b0-a84b-dc746d39afca"


def _add_subject(db_manager, keycloak_sub: str, *, realm: str = "portal", subject_id=None):
    session = db_manager.get_session()
    try:
        sub = Subject(keycloak_sub=keycloak_sub, keycloak_realm=realm)
        if subject_id is not None:
            # setattr, not `sub.id = …`: the ORM attribute types as `Column[UUID]`
            # and ty refuses a plain UUID. Same workaround models.py uses for
            # `tenant.kind` / `tenant.byo_mode` in ensure_tenant.
            setattr(sub, "id", uuid.UUID(str(subject_id)))
        session.add(sub)
        session.commit()
        session.refresh(sub)
        session.expunge(sub)
        return sub
    finally:
        session.close()


def _add_tenant_raw(db_manager, tenant_id: str, name: str, kind: str = "internal") -> None:
    """INSERT straight past `ensure_tenant`.

    Deliberately raw: the guard in `ensure_tenant` would refuse these rows, and
    what the DETECTOR must be proved against is a row that got in anyway — which
    is exactly how the live defect arrived (migration 013 INSERTs the literal id).
    """
    with db_manager.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, name, kind, byo_mode, owner_key, created_at) "
                "VALUES (:i, :n, :k, 'local_allowed', 'platform', now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"i": tenant_id, "n": name, "k": kind},
        )


def _snapshot(db_manager):
    session = db_manager.get_session()
    try:
        return collect_snapshot(session)
    finally:
        session.close()


def _samples(family):
    return {tuple(s.labels.values()): s.value for s in family.samples}


def _families(collector):
    return {f.name: f for f in collector.collect()}


class TestTheDetectorSeesBothClasses:
    def test_a_keycloak_sub_registered_as_a_tenant_is_detected(self, db_manager):
        """Class A — the live defect. The subject exists first; someone then
        registers that principal's Keycloak `sub` as an organisation."""
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 1
        # Not double-counted into the other class.
        assert snap.counts[(CLASS_VIOLATION, MATCH_SUBJECT_ID)] == 0
        assert snap.counts[(CLASS_SENTINEL, MATCH_KEYCLOAK_SUB)] == 0

        hit = [r for r in snap.rows if r.tenant_id == PROD_VIOLATOR]
        assert len(hit) == 1
        assert hit[0].match_kind == MATCH_KEYCLOAK_SUB
        assert hit[0].tenant_name == "some-colliding-tenant"
        assert hit[0].keycloak_realm == "portal"

    def test_a_subject_primary_key_registered_as_a_tenant_is_detected(self, db_manager):
        """Class B — this registry's OWN `subject.id`, not a Keycloak claim.

        A `keycloak_sub`-only predicate reads 0 here, which is precisely why the
        two are separate series rather than one number."""
        sid = "8382f0b3-c082-4f97-8d47-120ce5a18e05"
        _add_subject(db_manager, "some-other-keycloak-sub", subject_id=sid)
        _add_tenant_raw(db_manager, sid, "looks-like-an-org")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_SUBJECT_ID)] == 1
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 0
        assert [r.match_kind for r in snap.rows] == [MATCH_SUBJECT_ID]

    def test_an_id_that_is_both_a_sub_and_a_subject_pk_is_counted_under_both(self, db_manager):
        """One JOIN with an `OR` would collapse these into a single row and
        under-report. The UNION ALL is what keeps both causes visible."""
        shared = str(uuid.uuid4())
        _add_subject(db_manager, shared, subject_id=str(uuid.uuid4()))  # sub == the id
        _add_subject(db_manager, "unrelated-sub", subject_id=shared)  # pk == the id
        _add_tenant_raw(db_manager, shared, "double-trouble")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 1
        assert snap.counts[(CLASS_VIOLATION, MATCH_SUBJECT_ID)] == 1

    def test_the_predicate_is_not_defeated_by_case(self, db_manager):
        """`keycloak_sub` is free text (TEXT on prod, String(255) in the ORM) and
        `uuid::text` renders lower-case, so an upper-case sub would slip a naive
        `t.id::text = s.keycloak_sub` straight through."""
        tid = str(uuid.uuid4())
        _add_subject(db_manager, tid.upper())
        _add_tenant_raw(db_manager, tid, "shouty-sub")

        assert _snapshot(db_manager).counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 1


class TestTheRealShapedNegative:
    def test_a_genuine_v4_tenant_that_is_not_a_subject_is_not_flagged(self, db_manager):
        """UUID VERSION IS NOT A DISCRIMINATOR. `george-dekker`, `merqi` and
        `nonagentic-gtm` are genuine tenants with v4 ids — the same shape as a
        Keycloak sub. A detector that keyed on version would condemn them.

        Mutant that turns this RED: match on `uuid_version(t.id) = 4` instead of
        on the join."""
        george = str(uuid.uuid4())
        assert george[14] == "4", "this negative control must be a v4 uuid to be meaningful"
        _add_tenant_raw(db_manager, george, "george-dekker", kind="customer")
        _add_tenant_raw(db_manager, str(uuid.uuid4()), "merqi", kind="customer")
        # …and real, unrelated subjects exist at the same time, so the negative is
        # "no MATCH", not "no subjects".
        _add_subject(db_manager, str(uuid.uuid4()), realm="portal")
        _add_subject(db_manager, str(uuid.uuid4()), realm="secure-apps")

        snap = _snapshot(db_manager)
        assert snap.rows == []
        # Seeded, not absent: `== 0` must be a provable statement.
        for match_kind in (MATCH_KEYCLOAK_SUB, MATCH_SUBJECT_ID):
            assert snap.counts[(CLASS_VIOLATION, match_kind)] == 0
            assert snap.counts[(CLASS_SENTINEL, match_kind)] == 0

    def test_a_subject_with_no_tenant_row_is_not_a_violation(self, db_manager):
        """The normal case: 254 subjects exist and none of them is an org. A
        detector that flagged a bare subject would be all noise."""
        _add_subject(db_manager, str(uuid.uuid4()))
        assert _snapshot(db_manager).rows == []


class TestTheSentinelIsSplitOff:
    def test_default_anon_lands_in_the_sentinel_series_and_not_the_violation_series(
        self, db_manager
    ):
        """THE metric decision. `default-anon` is deliberate and PERMANENT — it
        is in `models.PLATFORM_TENANTS` and is re-seeded on every boot. If it
        shared a series with real violations the actionable gauge could never
        reach 0, so any alert on it would fire forever and be silenced.

        Mutant that turns this RED: drop the `class` label, or move the sentinel
        into `class="violation"`."""
        _add_subject(db_manager, SENTINEL_ID, realm="secure-apps")
        _add_tenant_raw(db_manager, SENTINEL_ID, "default-anon", kind="internal")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_SENTINEL, MATCH_KEYCLOAK_SUB)] == 1
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 0

        # …and it STILL MATCHES. The predicate is not loosened to achieve the
        # split: the sentinel is classified, never filtered out.
        assert [r.tenant_id for r in snap.rows] == [SENTINEL_ID]
        assert snap.rows[0].series_class == CLASS_SENTINEL

    def test_the_violation_series_can_reach_zero_with_the_sentinel_present(self, db_manager):
        """The whole point, stated as an assertion: sentinel present, actionable
        count 0. That is the reading an alert must be able to make."""
        _add_subject(db_manager, SENTINEL_ID)
        _add_tenant_raw(db_manager, SENTINEL_ID, "default-anon")

        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))
        rows = _samples(fams["nonagentic_license_tenant_is_subject_rows"])
        assert rows[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB, SOURCE_LICENCES_SUBJECT)] == 0.0
        assert rows[(CLASS_VIOLATION, MATCH_SUBJECT_ID, SOURCE_LICENCES_SUBJECT)] == 0.0
        assert rows[(CLASS_SENTINEL, MATCH_KEYCLOAK_SUB, SOURCE_LICENCES_SUBJECT)] == 1.0

    def test_the_exemption_register_holds_exactly_the_one_documented_id(self):
        """Adding an id here is a permanent claim that an identity is
        deliberately both an org and a subject, so it must cost a code review —
        this test is that cost."""
        assert SENTINEL_TENANT_IDS == frozenset({SENTINEL_ID})


class TestTheRetiredPrincipalIsARecordedException:
    """A REGISTERED id is routed OFF the alerting series; nothing else is.

    The register shipped EMPTY since migration 046 re-keyed its only entry
    (`portal-render-e2e`), so the mechanism is proved against a synthetic id the
    `retired` fixture registers. An exception must cover the `keycloak_sub` match
    only and must never hide a row.
    """

    @pytest.fixture
    def retired(self, monkeypatch):
        monkeypatch.setitem(
            RETIRED_PRINCIPAL_TENANT_IDS, RETIRED_PRINCIPAL_ID, "synthetic — test only"
        )

    def test_an_arbitrary_colliding_tenant_is_still_a_violation(self, db_manager):
        """Mutant that turns this RED: make `classify` / the SQL CASE route every
        non-sentinel id to `retired-principal`, or drop the register lookup."""
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 1
        assert snap.counts[(CLASS_RETIRED_PRINCIPAL, MATCH_KEYCLOAK_SUB)] == 0
        assert [(r.tenant_id, r.series_class) for r in snap.rows] == [
            (PROD_VIOLATOR, CLASS_VIOLATION)
        ]

    def test_a_registered_id_is_a_retired_principal_and_not_a_violation(self, db_manager, retired):
        """Both arms are asserted: the unbounded SQL count (what the alert reads)
        and the per-row class (what names the tenant). Mutant: delete the
        `retired` WHEN from `_COUNT_BY_CLASS` -> the count lands in violation;
        delete the branch in `classify` -> the row's class does."""
        _add_subject(db_manager, RETIRED_PRINCIPAL_ID, realm="portal")
        _add_tenant_raw(db_manager, RETIRED_PRINCIPAL_ID, "some-retired-tenant")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_RETIRED_PRINCIPAL, MATCH_KEYCLOAK_SUB)] == 1
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 0
        assert snap.counts[(CLASS_SENTINEL, MATCH_KEYCLOAK_SUB)] == 0
        # Still MATCHED and still NAMED — routed, never filtered out.
        assert [(r.tenant_id, r.series_class) for r in snap.rows] == [
            (RETIRED_PRINCIPAL_ID, CLASS_RETIRED_PRINCIPAL)
        ]

    def test_the_gauge_carries_it_under_its_own_class(self, db_manager, retired):
        _add_subject(db_manager, RETIRED_PRINCIPAL_ID, realm="portal")
        _add_tenant_raw(db_manager, RETIRED_PRINCIPAL_ID, "some-retired-tenant")
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))
        rows = _samples(fams["nonagentic_license_tenant_is_subject_rows"])
        assert rows[("retired-principal", MATCH_KEYCLOAK_SUB, SOURCE_LICENCES_SUBJECT)] == 1.0
        assert rows[("violation", MATCH_KEYCLOAK_SUB, SOURCE_LICENCES_SUBJECT)] == 1.0
        # Seeded at zero for the match class it does not cover.
        assert rows[("retired-principal", MATCH_SUBJECT_ID, SOURCE_LICENCES_SUBJECT)] == 0.0
        named = _samples(fams["nonagentic_license_tenant_is_subject"])
        assert {k[0]: k[3] for k in named} == {
            RETIRED_PRINCIPAL_ID: "retired-principal",
            PROD_VIOLATOR: "violation",
        }

    def test_the_exception_does_not_cover_a_subject_id_collision(self, db_manager, retired):
        """The recorded reason is "the Keycloak principal is gone". The same id
        turning up as this registry's own `subject.id` is a different defect and
        must stay actionable. Mutant: drop the `match_kind` condition."""
        _add_subject(db_manager, "kc-sub-unrelated", subject_id=RETIRED_PRINCIPAL_ID)
        _add_tenant_raw(db_manager, RETIRED_PRINCIPAL_ID, "some-retired-tenant")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_SUBJECT_ID)] == 1
        assert snap.counts[(CLASS_RETIRED_PRINCIPAL, MATCH_SUBJECT_ID)] == 0
        assert [r.series_class for r in snap.rows] == [CLASS_VIOLATION]

    def test_it_is_not_actionable_at_boot_but_is_still_said(self, db_manager, retired, caplog):
        _add_subject(db_manager, RETIRED_PRINCIPAL_ID, realm="portal")
        _add_tenant_raw(db_manager, RETIRED_PRINCIPAL_ID, "some-retired-tenant")

        with caplog.at_level(logging.WARNING):
            n = report_at_boot(db_manager.get_session)

        assert n == 0, "a recorded exception must not be counted as actionable"
        said = [r for r in caplog.records if RETIRED_PRINCIPAL_ID in r.getMessage()]
        assert [r.levelno for r in said] == [logging.WARNING]
        assert "RECORDED EXCEPTION" in said[0].getMessage()

    def test_the_register_is_empty_since_both_offenders_were_rekeyed(self):
        """Adding an id here silences an alert for it, so it costs a code review
        and a written reason — this test is that cost. Both ids that were ever
        candidates were REPAIRED instead (migrations 045 and 046)."""
        assert RETIRED_PRINCIPAL_TENANT_IDS == {}

    def test_the_old_portal_render_e2e_id_on_a_tenant_row_is_a_plain_violation(self, db_manager):
        """The state BEFORE migration 046, and the state a regression would
        recreate: the principal's sub registered as the tenant. It was a recorded
        exception for a few hours on 2026-10-06; it is actionable again."""
        _add_subject(db_manager, PORTAL_RENDER_E2E_PRINCIPAL_SUB, realm="portal")
        _add_tenant_raw(db_manager, PORTAL_RENDER_E2E_PRINCIPAL_SUB, "portal-render-e2e")

        snap = _snapshot(db_manager)
        assert snap.counts[(CLASS_VIOLATION, MATCH_KEYCLOAK_SUB)] == 1
        assert snap.counts[(CLASS_RETIRED_PRINCIPAL, MATCH_KEYCLOAK_SUB)] == 0
        assert [(r.tenant_id, r.series_class) for r in snap.rows] == [
            (PORTAL_RENDER_E2E_PRINCIPAL_SUB, CLASS_VIOLATION)
        ]
        assert report_at_boot(db_manager.get_session) == 1

    def test_the_rekeyed_portal_render_e2e_tenant_is_clean(self, db_manager):
        """The state AFTER 046: the tenant on its uuid7, the principal still a
        subject under its unchanged sub. Nothing matches in any class."""
        _add_subject(db_manager, PORTAL_RENDER_E2E_PRINCIPAL_SUB, realm="portal")
        _add_tenant_raw(db_manager, PORTAL_RENDER_E2E_TENANT_ID, "portal-render-e2e")

        snap = _snapshot(db_manager)
        assert snap.rows == []
        assert set(snap.counts.values()) == {0}
        assert report_at_boot(db_manager.get_session) == 0
        with db_manager.engine.connect() as conn:
            assert subject_for_tenant_id(conn, PORTAL_RENDER_E2E_TENANT_ID) is None
            hit = subject_for_tenant_id(conn, PORTAL_RENDER_E2E_PRINCIPAL_SUB)
        assert hit is not None and hit[2] == MATCH_KEYCLOAK_SUB, "the old id stays un-registerable"


class TestTheGaugeSurface:
    def test_the_offending_tenant_is_named_so_an_alert_is_actionable(self, db_manager):
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))
        named = _samples(fams["nonagentic_license_tenant_is_subject"])
        key = next(k for k in named if k[0] == PROD_VIOLATOR)
        assert key[1] == "some-colliding-tenant"
        assert key[3] == CLASS_VIOLATION
        assert key[4] == MATCH_KEYCLOAK_SUB
        assert key[5] == SOURCE_LICENCES_SUBJECT
        assert named[key] == 1.0

    def test_a_database_fault_emits_collector_up_zero_and_no_other_series(self):
        """The empty-but-confident scrape is the failure this guards: a violation
        count of 0 produced by a FAILED query is indistinguishable from a clean
        estate, so `{class="violation"} == 0` would read TRUE while blind."""

        class _Dead:
            def execute(self, *a, **kw):
                raise RuntimeError("connection refused")

            def close(self):
                pass

        fams = _families(TenantSubjectCollector(lambda: _Dead(), ttl=0.0))
        assert _samples(fams["nonagentic_license_tenant_subject_collector_up"]) == {(): 0.0}
        assert "nonagentic_license_tenant_is_subject_rows" not in fams
        assert "nonagentic_license_tenant_is_subject" not in fams
        assert "nonagentic_license_tenant_is_subject_source_measured" not in fams

    def test_a_healthy_snapshot_reports_collector_up_one(self, db_manager):
        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))
        assert _samples(fams["nonagentic_license_tenant_subject_collector_up"]) == {(): 1.0}


class TestEnsureTenantRefusesNewRegistrations:
    def test_registering_a_keycloak_sub_as_a_tenant_is_refused(self, db_manager):
        """The generator fix. `POST /tenants/{id}` and every `provision_*` script
        go through this primitive."""
        sub = _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        with pytest.raises(ClientSafeError) as exc:
            db_manager.ensure_tenant(
                PROD_VIOLATOR, name="some-colliding-tenant", owner_key="platform"
            )
        assert str(sub.id) in str(exc.value)
        assert "keycloak_sub" in str(exc.value)

        # …and nothing was written.
        with db_manager.engine.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM tenants WHERE id = :i"), {"i": PROD_VIOLATOR}
            ).scalar_one()
        assert n == 0

    def test_registering_a_subject_primary_key_as_a_tenant_is_refused(self, db_manager):
        sid = str(uuid.uuid4())
        _add_subject(db_manager, "an-unrelated-sub", subject_id=sid)
        with pytest.raises(ClientSafeError) as exc:
            db_manager.ensure_tenant(sid, name="not-an-org", owner_key="platform")
        assert "subject_id" in str(exc.value)

    def test_a_legitimate_v4_tenant_is_still_registerable(self, db_manager):
        """The guard must not make ordinary provisioning fail — `george-dekker`
        is a v4 id and a genuine org."""
        _add_subject(db_manager, str(uuid.uuid4()))
        tid = str(uuid.uuid4())
        tenant = db_manager.ensure_tenant(tid, name="george-dekker", owner_key="platform")
        assert str(tenant.id) == tid

    def test_an_existing_violating_row_is_still_returned_not_refused(self, db_manager):
        """⛔ THE BOOT-SAFETY PROPERTY. `seed_platform_tenants()` re-registers
        every PLATFORM_TENANTS id on EVERY boot, and two of them violate the
        invariant on prod. The guard sits AFTER the idempotent early return, so a
        pre-existing row is returned untouched; refusing it would CrashLoop both
        replicas of the platform auth path.

        Mutant that turns this RED: move the guard above the `existing is not
        None` return."""
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        tenant = db_manager.ensure_tenant(
            PROD_VIOLATOR, name="some-colliding-tenant", owner_key="platform"
        )
        assert str(tenant.id) == PROD_VIOLATOR

    def test_the_write_guard_and_the_gauge_share_one_predicate(self, db_manager):
        """They must never drift into disagreeing about what a violation is —
        `models.ensure_tenant` imports `subject_for_tenant_id` rather than
        restating the join."""
        sub = _add_subject(db_manager, PROD_VIOLATOR)
        session = db_manager.get_session()
        try:
            hit = subject_for_tenant_id(session, PROD_VIOLATOR)
        finally:
            session.close()
        assert hit is not None
        assert hit[0] == str(sub.id)
        assert hit[2] == MATCH_KEYCLOAK_SUB


class TestNothingIsFatalOnAnExistingRow:
    def test_report_at_boot_logs_each_violation_and_returns_the_actionable_count(
        self, db_manager, caplog
    ):
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")
        _add_subject(db_manager, SENTINEL_ID)
        _add_tenant_raw(db_manager, SENTINEL_ID, "default-anon")

        with caplog.at_level(logging.ERROR):
            n = report_at_boot(db_manager.get_session)

        assert n == 1, "the sentinel must not be counted as actionable"
        assert PROD_VIOLATOR in caplog.text
        assert SENTINEL_ID not in caplog.text

    def test_report_at_boot_never_raises_on_a_database_fault(self, caplog):
        """It runs inside `_run_db_bootstrap`, where anything that is not a
        connect fault propagates and the pod REFUSES TO SERVE. A hygiene report
        may never be the thing that takes the auth path down."""

        def _explode():
            raise RuntimeError("connection refused")

        with caplog.at_level(logging.ERROR):
            assert report_at_boot(_explode) == 0

    def test_report_at_boot_on_a_clean_estate_returns_zero(self, db_manager):
        _add_subject(db_manager, str(uuid.uuid4()))
        assert report_at_boot(db_manager.get_session) == 0


class TestMigration035:
    def test_it_ships_a_downgrade_as_the_repo_convention_requires(self):
        """Repo convention: every migration ships migrations/downgrades/<same
        name>. `.gitignore` blanket-ignores `*.sql` and un-ignores
        `migrations/*.sql` and `migrations/downgrades/*.sql` separately, so a
        downgrade in any other directory is silently untracked."""
        assert UP.exists()
        assert DOWN.exists()
        assert DOWN.parent.name == "downgrades"

    def test_it_runs_against_a_database_that_already_violates_without_raising(self, db_manager):
        """⛔ Three rows violate on prod. A migration that failed on them would
        raise out of `_run_db_bootstrap` and the pod would refuse to serve.
        Unlike 033 — whose CHECKs had zero violating rows to land on — there is
        nothing here that can be made safely fatal.

        Mutant that turns this RED: change the file's RAISE WARNING to RAISE
        EXCEPTION."""
        _add_subject(db_manager, PROD_VIOLATOR, realm="portal")
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")
        _add_subject(db_manager, SENTINEL_ID)
        _add_tenant_raw(db_manager, SENTINEL_ID, "default-anon")

        from scripts.apply_migrations import split_statements

        stmts = split_statements(UP.read_text(encoding="utf-8"))
        # ⛔ Assert the list is NON-EMPTY first. A loop over `[]` executes nothing
        # and would let this test pass green while the migration was never run —
        # the exact way a mutation to `split_statements` (or a renamed UP file)
        # could disarm the only proof that 035 is safe on a violating database.
        assert stmts, "split_statements returned no statements — nothing was exercised"

        with db_manager.engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))

        # And it is report-only: both violating rows are still there afterwards.
        with db_manager.engine.connect() as conn:
            survived = conn.execute(
                text("SELECT count(*) FROM tenants WHERE id::text = ANY(:ids)"),
                {"ids": [PROD_VIOLATOR, SENTINEL_ID]},
            ).scalar_one()
        assert survived == 2, "035 must not remove or repoint a row it only reports"

    def test_it_is_idempotent_because_it_writes_nothing(self, db_manager):
        """Report-only: a second apply must be as harmless as the first, and no
        row may move."""
        _add_subject(db_manager, PROD_VIOLATOR)
        _add_tenant_raw(db_manager, PROD_VIOLATOR, "some-colliding-tenant")

        from scripts.apply_migrations import split_statements

        stmts = split_statements(UP.read_text(encoding="utf-8"))
        # Same guard as the test above: the row counts below hold whether or not
        # the migration ran, so without this the whole test survives `stmts == []`.
        assert stmts, "split_statements returned no statements — nothing was exercised"
        for _ in range(2):
            with db_manager.engine.begin() as conn:
                for stmt in stmts:
                    conn.execute(text(stmt))

        with db_manager.engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 3
            assert conn.execute(text("SELECT count(*) FROM subject")).scalar_one() == 1

    def test_it_adds_no_constraint_and_no_trigger(self):
        """The decision, pinned. A CHECK cannot express a cross-table predicate,
        and a trigger was rejected on a measured fact: `seed_platform_tenants()`
        re-INSERTs two of the three violating ids on EVERY boot under `ON
        CONFLICT DO NOTHING`, and a BEFORE INSERT row trigger fires BEFORE
        conflict arbitration — so it would raise on every boot of the service
        backing ext_authz for the whole platform at replicas: 2.

        If a future change genuinely wants one of these, it must delete this test
        and say why in the same diff."""
        body = UP.read_text(encoding="utf-8").upper()
        assert "ADD CONSTRAINT" not in body
        assert "CREATE TRIGGER" not in body
        assert "RAISE EXCEPTION" not in body


class TestTheBlindSpotIsDeclared:
    """⛔ A SINGLE-SOURCE DETECTOR IS BLIND BY CONSTRUCTION, and this one is
    single-source. The union of offending ids across the two doors is FOUR;
    the licences-DB door sees three of them. `nonagentic-gtm` is a LIVE Keycloak
    principal registered as a tenant that was never written to `subject`, so no
    predicate in this module can match it.

    A documented blind spot is acceptable. An UNDOCUMENTED one is what produced
    the defect, so it is asserted here rather than left in prose."""

    def test_the_unmeasured_source_reports_zero_coverage_not_zero_violations(self, db_manager):
        """The distinction the whole design turns on: a seeded 0 is right for
        "measured, found none" and WRONG for "never looked". The Keycloak door is
        known to be non-zero (`nonagentic-gtm`), so it must NOT appear in the
        violation gauge at all — it appears in the coverage gauge as 0.

        Mutant that turns this RED: seed `source="keycloak_principal"` into
        `nonagentic_license_tenant_is_subject_rows` at 0."""
        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))

        measured = _samples(fams["nonagentic_license_tenant_is_subject_source_measured"])
        assert measured[(SOURCE_LICENCES_SUBJECT,)] == 1.0
        assert measured[(SOURCE_KEYCLOAK_PRINCIPAL,)] == 0.0

        rows = _samples(fams["nonagentic_license_tenant_is_subject_rows"])
        assert {k[2] for k in rows} == {SOURCE_LICENCES_SUBJECT}, (
            "the unmeasured Keycloak door must not carry a violation count — a zero "
            "there would claim a measurement nobody took"
        )

    def test_the_known_blind_spot_is_named_and_warned_about_on_every_boot(self, db_manager, caplog):
        """Announced even on a CLEAN estate, because a clean reading is exactly
        when a reader concludes "no problem"."""
        with caplog.at_level(logging.WARNING):
            assert report_at_boot(db_manager.get_session) == 0
        assert "e4f70a83-81ee-429a-abca-6999e3a0d24f" in caplog.text
        assert "nonagentic-gtm" in caplog.text
        assert "BLIND SPOT" in caplog.text

    def test_the_blind_spot_register_names_nonagentic_gtm(self):
        """It is a transcribed 2026-09-10 measurement, deliberately NOT a gauge —
        emitting it as a series would dress a frozen reading up as a live one."""
        ids = {i for i, _ in KNOWN_KEYCLOAK_ONLY_BLIND_SPOTS}
        assert "e4f70a83-81ee-429a-abca-6999e3a0d24f" in ids

    def test_the_migration_declares_the_same_blind_spot(self):
        """The SQL half is read by a different audience (whoever tails the apply
        log) and must not be quietly narrower than the Python half."""
        body = UP.read_text(encoding="utf-8")
        assert "e4f70a83-81ee-429a-abca-6999e3a0d24f" in body
        assert "BLIND SPOT" in body


class TestPhrasingIsNotPerson:
    def test_the_gauge_help_text_says_principal_not_person(self, db_manager):
        """Three of the four offending ids are SERVICE ACCOUNTS. Help text that
        said "a person" would make them read as false positives and get the
        alert dismissed.

        Mutant that turns this RED: reword the help text back to "a person's
        identity"."""
        fams = _families(TenantSubjectCollector(db_manager.get_session, ttl=0.0))
        help_text = fams["nonagentic_license_tenant_is_subject_rows"].documentation
        assert "principal" in help_text.lower()
        assert "person: " in help_text.lower() or "not person" in help_text.lower()


class TestTheGaugeIsActuallyExposed:
    """⚠️ A COLLECTOR THAT IS NOT REGISTERED EMITS NOTHING, and every test above
    would still pass. `collect()` returning the right families proves the
    row→series mapping; it does not prove the series reach a scrape. This drives
    the real route.

    Mutant that turns this RED: delete the
    `_expiry_registry.register(TenantSubjectCollector(...))` line in main.py.

    ⚠️ WHAT THIS DELIBERATELY DOES NOT ASSERT, and why. Under TestClient the
    registered collector holds `main.db_manager.get_session` — the MODULE-LEVEL
    manager bound at import against `DATABASE_URL`, not the per-test engine — so
    its snapshot fails and only `collector_up 0` is emitted. That is a
    pre-existing property of this harness shared by the sibling expiry collector,
    NOT a defect in either: the value series are proved against a real database
    in the classes above. Asserting values here would mean asserting the harness.

    It also does not prove Prometheus SCRAPES the route — that needs the
    `prometheus.io/scrape` pod annotation, a gitops change documented on
    `services/expiry_metrics.py`.
    """

    def test_the_collector_is_registered_on_the_metrics_route(self, client):
        body = client.get("/metrics").text
        assert "nonagentic_license_tenant_subject_collector_up" in body, (
            "the tenant/subject collector is not on the /metrics registry at all — "
            "every collect() test above would still pass with it unregistered"
        )
        # The sibling expiry collector must still be there too: registering a
        # second collector on a shared registry is exactly where one gets lost.
        assert "nonagentic_license_expiry_collector_up" in body


class TestTheBootReportIsWiredIn:
    """A report nobody calls is a report that never runs, and every test above
    would still pass. This pins the CALL SITE and its ordering."""

    def test_run_db_bootstrap_reports_the_invariant_last(self, monkeypatch):
        """LAST, after the seed: the seed itself re-INSERTs two of the three ids
        that match on prod, so a report placed before it would read a different
        database than the one the pod goes on to serve.

        Mutant that turns this RED: delete the
        `report_tenant_subject_violations(...)` call in `_run_db_bootstrap`."""
        import main

        calls: list[str] = []
        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: False)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: calls.append("create"))
        monkeypatch.setattr(
            main.db_manager, "seed_platform_tenants", lambda: calls.append("seed") or 0
        )
        monkeypatch.setattr(
            "scripts.apply_migrations.run_from_env", lambda: calls.append("migrate") or 0
        )
        monkeypatch.setattr(
            main, "report_tenant_subject_violations", lambda _f: calls.append("report") or 0
        )

        main._run_db_bootstrap()

        assert calls == ["create", "migrate", "seed", "report"], (
            f"bootstrap order changed: {calls}. The tenant/subject report must run "
            f"AFTER seed_platform_tenants — the seed re-inserts ids that match the "
            f"invariant, so a report before it reads a database the pod never serves."
        )

    def test_a_failing_report_cannot_stop_the_pod_serving(self, monkeypatch):
        """⛔ THE OPPOSITE OF `test_a_seed_failure_is_not_swallowed`, and
        deliberately so. A seed failure means a missing platform identity and
        must refuse to serve. A hygiene REPORT failing means only that we cannot
        currently measure a weeks-old billing defect — refusing to serve on that
        would CrashLoop both replicas of the platform auth path.

        Mutant that turns this RED: let `report_at_boot` propagate."""
        import main

        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: False)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)
        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", lambda: 0)

        def boom():
            raise RuntimeError("the invariant query exploded")

        monkeypatch.setattr(main.db_manager, "get_session", boom)
        main._run_db_bootstrap()  # must not raise
