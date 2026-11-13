"""Per-seat billing for CommonPractice: a PERIOD seat count per APP, and a Subject realm the
minting service states instead of inheriting the caller's.

⭐ PERIOD. CommonPractice bills "distinct employees who held an active licence at ANY point in
the period". The point-in-time count excludes a revoked row by its CURRENT status, so a member
offboarded on the 20th vanished from the whole month. ``active_during_clauses`` answers the period
question; the shared point-in-time predicate is NOT changed (``test_seat_predicate_is_shared``).

⭐ APP. Without ``app_key`` a tenant's licences for every other product inflated one product's
seat count.

⛔ REALM. ``create_user_license`` stamped a NEW Subject with the CALLER's realm. CommonPractice's
service account lives in ``secure-apps``, so its employees became ``secure-apps`` Subjects — a
person counted as human only because ``NON_HUMAN_SUBJECT_REALMS`` happens to be ``("service",)``.

Every exclusion is paired with a row that must still count, so "excluded everything" fails too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from models import ClientSafeError, DatabaseManager, License, Subject
from tests.conftest import SEED_TENANT_ID

NOW = datetime.now(UTC)
P_START = NOW - timedelta(days=20)
P_END = NOW - timedelta(days=5)


def _lic(db: DatabaseManager, app, plan, sub: str) -> License:
    """A perpetual licence that has existed since well before the period (``created_at`` is
    backdated: a row minted NOW would sit after ``P_END`` and never be in the period at all)."""
    subject = db.create_or_get_subject(keycloak_sub=sub, keycloak_realm="portal")
    lic = db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=SEED_TENANT_ID,
        status="active",
        starts_at=NOW - timedelta(days=60),
        expires_at=None,
    )
    _set(db, lic, created_at=NOW - timedelta(days=60))
    return lic


def _set(db: DatabaseManager, lic: License, **cols) -> None:
    session = db.get_session()
    try:
        row = session.query(License).filter(License.id == lic.id).first()
        assert row is not None
        for k, v in cols.items():
            setattr(row, k, v)
        session.commit()
    finally:
        session.close()


def _period(db: DatabaseManager, **kw) -> dict:
    out = db.count_tenant_seats(SEED_TENANT_ID, period_start=P_START, period_end=P_END, **kw)
    assert out is not None
    return out


class TestPeriodSeats:
    def test_a_member_revoked_inside_the_period_is_a_seat_for_that_period(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ The case the point-in-time count gets wrong. MUTATION TARGET: drop the revocation arm
        of ``active_during_clauses`` and this reads 1."""
        _lic(db_manager, seed_app, seed_plan, "stayer")
        leaver = _lic(db_manager, seed_app, seed_plan, "leaver")
        _set(db_manager, leaver, status="revoked", revoked_at=NOW - timedelta(days=10))

        out = _period(db_manager)
        assert out["human_subjects"] == 2
        assert out["basis"] == "active_during_period"
        # Control: the point-in-time count at period end does NOT see the leaver — the defect.
        assert db_manager.count_tenant_seats(SEED_TENANT_ID, as_of=P_END)["human_subjects"] == 1

    def test_a_member_rehired_inside_the_period_is_one_person_but_two_licences(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ Billing bills ``human_subjects`` for a period term, never ``seats``. An employee
        offboarded and re-invited inside the month holds TWO licence rows over the period; they are
        one person. Pins both halves so a reader switching fields sees the difference."""
        first = _lic(db_manager, seed_app, seed_plan, "rehired")
        _set(db_manager, first, status="revoked", revoked_at=NOW - timedelta(days=15))
        second = _lic(db_manager, seed_app, seed_plan, "rehired")
        _set(
            db_manager,
            second,
            created_at=NOW - timedelta(days=12),
            starts_at=NOW - timedelta(days=12),
        )

        out = _period(db_manager)
        assert out["seats"] == 2  # licence rows
        assert out["human_subjects"] == 1  # the billed basis

    def test_a_member_revoked_before_the_period_is_not_a_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        _lic(db_manager, seed_app, seed_plan, "stayer2")
        gone = _lic(db_manager, seed_app, seed_plan, "long-gone")
        _set(db_manager, gone, status="revoked", revoked_at=NOW - timedelta(days=30))
        assert _period(db_manager)["human_subjects"] == 1

    def test_a_member_who_joined_after_the_period_is_not_a_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        _lic(db_manager, seed_app, seed_plan, "stayer3")
        late = _lic(db_manager, seed_app, seed_plan, "late-joiner")
        _set(
            db_manager, late, created_at=NOW - timedelta(days=2), starts_at=NOW - timedelta(days=2)
        )
        assert _period(db_manager)["human_subjects"] == 1

    def test_a_member_who_joined_inside_the_period_is_a_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        joiner = _lic(db_manager, seed_app, seed_plan, "mid-joiner")
        _set(
            db_manager,
            joiner,
            created_at=NOW - timedelta(days=12),
            starts_at=NOW - timedelta(days=12),
        )
        assert _period(db_manager)["human_subjects"] == 1

    def test_period_bounds_are_validated(self, db_manager) -> None:
        with pytest.raises(ClientSafeError):
            db_manager.count_tenant_seats(SEED_TENANT_ID, period_start=P_START)
        with pytest.raises(ClientSafeError):
            db_manager.count_tenant_seats(SEED_TENANT_ID, period_start=P_END, period_end=P_START)


class TestAppScope:
    def test_another_apps_licence_does_not_inflate_this_apps_seats(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        other = db_manager.create_app(
            key="other-product", name="Other", audience="human", issuers=("platform",)
        )
        other_plan = db_manager.create_plan(app_id=UUID(str(other.id)), code="p", name="P")
        _lic(db_manager, seed_app, seed_plan, "cp-member")
        _lic(db_manager, other, other_plan, "other-member")

        assert db_manager.count_tenant_seats(SEED_TENANT_ID)["human_subjects"] == 2  # control
        scoped = db_manager.count_tenant_seats(SEED_TENANT_ID, app_key="test-app")
        assert scoped["human_subjects"] == 1
        assert scoped["app_key"] == "test-app"

    def test_an_unknown_app_is_not_a_fabricated_zero(self, db_manager) -> None:
        assert db_manager.count_tenant_seats(SEED_TENANT_ID, app_key="no-such-app") == {
            "app_not_found": True,
            "app_key": "no-such-app",
        }

    def test_route_answers_unknown_app_with_422_never_404(
        self, auth_client_admin: TestClient, seed_app
    ) -> None:
        """⛔ billing's seats client reads 404 as a DURABLE 0. A typo must not become that."""
        r = auth_client_admin.get(f"/internal/tenants/{SEED_TENANT_ID}/seat-count?app=no-such-app")
        assert r.status_code == 422, r.text

    def test_route_period_and_app(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        leaver = _lic(db_manager, seed_app, seed_plan, "route-leaver")
        _set(db_manager, leaver, status="revoked", revoked_at=NOW - timedelta(days=10))
        r = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT_ID}/seat-count",
            params={
                "app": "test-app",
                "period_start": P_START.isoformat(),
                "period_end": P_END.isoformat(),
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["human_subjects"] == 1
        assert body["basis"] == "active_during_period"
        assert body["app_key"] == "test-app"

    def test_route_rejects_a_half_period(self, auth_client_admin: TestClient, seed_app) -> None:
        r = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT_ID}/seat-count",
            params={"period_start": P_START.isoformat()},
        )
        assert r.status_code == 422, r.text


def _realm_of(db: DatabaseManager, sub: str) -> str | None:
    session = db.get_session()
    try:
        row = session.query(Subject).filter(Subject.keycloak_sub == sub).first()
        return None if row is None else str(row.keycloak_realm)
    finally:
        session.close()


class TestSubjectRealm:
    def test_a_stated_realm_is_used_instead_of_the_callers(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """The admin caller authenticates in ``secure-apps``; the employee is a ``portal`` human."""
        body = {"app_id": str(seed_app.id), "plan_id": str(seed_plan.id), "subject_realm": "portal"}
        r = auth_client_admin.post("/licenses/user/cp-employee-sub", json=body)
        assert r.status_code == 201, r.text
        assert _realm_of(db_manager, "cp-employee-sub") == "portal"

    def test_without_it_the_legacy_caller_realm_is_kept(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        body = {"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)}
        r = auth_client_admin.post("/licenses/user/legacy-sub", json=body)
        assert r.status_code == 201, r.text
        assert _realm_of(db_manager, "legacy-sub") == "secure-apps"

    def test_a_non_human_realm_is_refused(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """Stating ``service`` would let a caller hide a person from the seat count."""
        body = {
            "app_id": str(seed_app.id),
            "plan_id": str(seed_plan.id),
            "subject_realm": "service",
        }
        r = auth_client_admin.post("/licenses/user/hidden-human", json=body)
        assert r.status_code == 422, r.text
        assert _realm_of(db_manager, "hidden-human") is None

    def test_an_existing_subject_keeps_its_realm(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        db_manager.create_or_get_subject(keycloak_sub="pre-existing", keycloak_realm="secure-apps")
        body = {"app_id": str(seed_app.id), "plan_id": str(seed_plan.id), "subject_realm": "portal"}
        r = auth_client_admin.post("/licenses/user/pre-existing", json=body)
        assert r.status_code == 201, r.text
        assert _realm_of(db_manager, "pre-existing") == "secure-apps"


@pytest.fixture
def script_db_url(monkeypatch, db_manager) -> str:
    """The script reads ``DATABASE_URL`` itself — point it at the SAME test database the fixtures
    use, so what it writes is what the assertions read back."""
    url = str(db_manager.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_URL", url)
    # The script builds its own `DatabaseManager`, which appends `sslmode=require` unless told
    # otherwise (`POSTGRES_SSLMODE`). The test database — ephemeral in CI, a container locally —
    # speaks no TLS, exactly as the CI archetype's Postgres does not.
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    return url


class TestRealmCorrectionScript:
    """``scripts/fix_subject_realm.py`` — the operator tool the mint path's
    ``subject_realm_mismatch`` warning assumes exists.

    ⭐ An existing Subject keeps its realm on a mint (pinned directly above), which is correct and
    also means a wrongly-stamped row can ONLY be corrected here. These pin the two properties that
    make that safe: a dry run writes nothing, and the tool refuses to write a realm that would
    remove the person from the seat count.
    """

    def test_a_dry_run_reports_and_writes_nothing(self, db_manager, script_db_url, capsys) -> None:
        from scripts.fix_subject_realm import main as fix_realm

        db_manager.create_or_get_subject(keycloak_sub="wrong-realm", keycloak_realm="secure-apps")
        rc = fix_realm(["--realm", "portal", "--subject", "wrong-realm"])
        assert rc == 0
        # ⚠️ MUTATION TARGET: make `--apply` the default and this row moves to portal here.
        assert _realm_of(db_manager, "wrong-realm") == "secure-apps"
        assert "would_set" in capsys.readouterr().out

    def test_apply_corrects_only_the_named_subject(self, db_manager, script_db_url) -> None:
        from scripts.fix_subject_realm import main as fix_realm

        db_manager.create_or_get_subject(keycloak_sub="cp-a", keycloak_realm="secure-apps")
        db_manager.create_or_get_subject(keycloak_sub="cp-b", keycloak_realm="secure-apps")
        rc = fix_realm(["--realm", "portal", "--subject", "cp-a", "--apply"])
        assert rc == 0
        assert _realm_of(db_manager, "cp-a") == "portal"
        # The bystander is untouched: this tool never SEARCHES for rows to fix.
        assert _realm_of(db_manager, "cp-b") == "secure-apps"

    def test_a_non_human_realm_is_refused(self, db_manager, script_db_url) -> None:
        from scripts.fix_subject_realm import main as fix_realm

        db_manager.create_or_get_subject(keycloak_sub="cp-c", keycloak_realm="portal")
        # ⛔ Writing `service` here would hide a person from `human_subjects` — an under-bill
        # produced by the very tool that exists to make the seat count right.
        assert fix_realm(["--realm", "service", "--subject", "cp-c", "--apply"]) == 2
        assert _realm_of(db_manager, "cp-c") == "portal"

    def test_an_unknown_subject_is_an_error_not_a_silent_success(
        self, db_manager, script_db_url
    ) -> None:
        from scripts.fix_subject_realm import main as fix_realm

        # Usually a Keycloak sub pasted where a Subject id was meant. Exiting 0 would report a run
        # that corrected nothing as a success.
        assert fix_realm(["--realm", "portal", "--subject", "no-such-subject", "--apply"]) == 2


class TestCpTenantSubjects:
    """⭐ CommonPractice binds a workspace's TENANT-level licence to ``cp-tenant:<tenantId>:a|b``,
    minted with ``subject_realm: "cp-tenant"``. They are not people: before ``cp-tenant`` joined
    ``NON_HUMAN_SUBJECT_REALMS`` each CP workspace billed phantom seats for them.

    Every exclusion is paired with a human in the SAME tenant that must still count."""

    @staticmethod
    def _mint(client: TestClient, app, plan, sub: str, realm: str):
        body = {"app_id": str(app.id), "plan_id": str(plan.id), "subject_realm": realm}
        return client.post(f"/licenses/user/{sub}", json=body)

    def test_create_with_realm_cp_tenant_succeeds(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        sub = f"cp-tenant:{SEED_TENANT_ID}:a"
        r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
        assert r.status_code == 201, r.text
        assert _realm_of(db_manager, sub) == "cp-tenant"

    def test_cp_tenant_licences_are_not_seats_but_a_human_still_is(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """MUTATION TARGET: drop ``cp-tenant`` from ``NON_HUMAN_SUBJECT_REALMS`` and
        ``human_subjects`` reads 3."""
        for slot in ("a", "b"):
            sub = f"cp-tenant:{SEED_TENANT_ID}:{slot}"
            r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
            assert r.status_code == 201, r.text
        r = self._mint(auth_client_admin, seed_app, seed_plan, "cp-real-human", "portal")
        assert r.status_code == 201, r.text

        r = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT_ID}/seat-count", params={"app": "test-app"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subjects"] == 3
        assert body["human_subjects"] == 1  # the human, and only the human
        assert body["service_subjects"] == 2
        parts = body["human_subjects"] + body["service_subjects"] + body["unresolved_subjects"]
        assert parts == body["subjects"]

    def test_cp_tenant_realm_cannot_hide_a_person(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """⛔ ``cp-tenant`` is accepted ONLY for a ``cp-tenant:``-named subject. Stated for a
        person's Keycloak sub it would launder that person out of the seat count."""
        sub = "5f0c2a8e-1111-4222-8333-944455556666"
        r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
        assert r.status_code == 422, r.text
        assert _realm_of(db_manager, sub) is None

    def test_a_sub_naming_another_tenant_is_refused(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """⛔ A prefix-only check let a caller mint unbilled licences under any name. The sub
        must carry THIS licence's tenant id."""
        sub = "cp-tenant:01999999-0000-7000-8000-000000000999:a"
        r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
        assert r.status_code == 422, r.text
        assert _realm_of(db_manager, sub) is None

    def test_a_third_slot_is_refused(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """Only slots ``a`` and ``b`` exist — a ``c`` would be an unbounded third unbilled holder."""
        sub = f"cp-tenant:{SEED_TENANT_ID}:c"
        r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
        assert r.status_code == 422, r.text
        assert _realm_of(db_manager, sub) is None

    def test_slot_b_for_own_tenant_succeeds(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        sub = f"cp-tenant:{SEED_TENANT_ID}:b"
        r = self._mint(auth_client_admin, seed_app, seed_plan, sub, "cp-tenant")
        assert r.status_code == 201, r.text
        assert _realm_of(db_manager, sub) == "cp-tenant"

    def test_service_is_still_refused(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        r = self._mint(auth_client_admin, seed_app, seed_plan, "svc:x:y", "service")
        assert r.status_code == 422, r.text

    def test_cp_tenant_is_not_a_bundle_holder(self) -> None:
        from models import is_externally_held_subject, is_tenant_level_subject

        assert is_tenant_level_subject(f"cp-tenant:{SEED_TENANT_ID}:a", "cp-tenant") is False
        assert is_externally_held_subject("cp-tenant") is True
        # Controls: the real bundle holders are unchanged.
        assert is_tenant_level_subject(f"svc:{SEED_TENANT_ID}:app", "service") is True
        assert is_tenant_level_subject("svc-workers-x", "service") is True
        assert is_tenant_level_subject("human", "portal") is False
