"""
Unit tests for main.py API endpoints.

Covers CRUD operations, error paths, delete endpoints, and edge cases.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from models import App
from tests.conftest import insert_legacy_perpetual_token
from tests.support.billing_contract import assert_satisfies, violations
from tests.support.sql_counter import count_statements


class TestHealthEndpoints:
    """Test health check endpoints."""

    def test_health_check(self, client: TestClient):
        """Test /health returns healthy status with all expected fields.

        ``database`` MUST read ``not_checked``. This assertion used to read
        ``connected`` and it passed for the only possible reason: the value was
        a hardcoded literal in a handler that opens no session. Until gitops
        #2553 all three k8s probes pointed here, so a pod with a dead database
        stayed Ready and kept serving platform auth. The value is now the truth
        about what the handler knows; ``/health/ready`` is where database truth
        lives (``test_health_ready`` below).
        """
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "licensing-service"
        assert data["version"] == "1.0.0"
        assert data["database"] == "not_checked", (
            "/health must not claim a database state it never checked; "
            "the real check is /health/ready"
        )
        assert isinstance(data["timestamp"], str)
        assert len(data["timestamp"]) > 0

    def test_health_issues_no_sql(self, client: TestClient, db_manager):
        """/health must issue ZERO SQL statements — this is the load-bearing half.

        The value assertion above survives a rename; THIS one survives someone
        "improving" /health into a real database check, which is the change that
        re-opens the DoS vector:

        * the Cloudflare tunnel routes public ``prefix: /licenses/`` to this
          service (``clusters/strato/istio/tunnel-mtls/virtualservice.yaml``)
          and ``/licenses/health`` aliases this handler;
        * ``auth/middleware.py`` PUBLIC_ENDPOINTS exempts both from auth, so the
          path is internet-reachable AND unauthenticated;
        * ``models.py`` builds the engine with ``poolclass=NullPool``, so every
          session is a fresh connection against a pgbouncer pool with 40 server
          connections fleet-wide.

        A query here is therefore an unauthenticated connection-exhaustion
        amplifier aimed at the pool the licences auth path depends on.
        """
        with count_statements(db_manager.engine) as rec:
            response = client.get("/health")

        assert response.status_code == 200
        assert sum(rec.verbs.values()) == 0, (
            "/health opened the database — it is a public unauthenticated endpoint on a "
            "NullPool engine; put the check in /health/ready instead. Statements issued:\n"
            + "\n".join(rec.statements)
        )

    def test_the_sql_counter_is_not_vacuous(self, client: TestClient, db_manager):
        """Positive control for the assertion above.

        A counter wired to the wrong engine, or a client whose db_manager is not
        the fixture's, would report zero statements for EVERY request and make
        ``test_health_issues_no_sql`` vacuously green. /health/ready runs one
        ``SELECT 1`` on the same engine, so it must be seen.
        """
        with count_statements(db_manager.engine) as rec:
            response = client.get("/health/ready")

        assert response.status_code == 200
        assert rec.verbs["select"] >= 1, (
            "the counter saw no SELECT on a request that provably issues one — "
            f"it is not observing the engine under test: {rec.statements}"
        )

    def test_licenses_health_alias(self, client: TestClient):
        """`/licenses/health` aliases /health for callers that traverse the
        api-external no-rewrite VS (stage-gate-check.sh) — must be public
        (not gated by AuthMiddleware) and return the same shape as /health.

        It inherits /health's honesty for free (``return await health_check()``),
        which is asserted here so a future divergence is caught: this is the
        INTERNET-REACHABLE spelling of the endpoint, so if either one is going to
        drift back into querying the database it is this one that matters."""
        response = client.get("/licenses/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "licensing-service"
        assert data["database"] == "not_checked"

    def test_health_ready(self, client: TestClient):
        """/health/ready is the ONLY endpoint that reports database truth."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["database"] == "connected"

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("DB connection refused"),
            OperationalError("SELECT 1", {}, Exception("could not connect to server")),
        ],
        ids=["runtime_error", "sqlalchemy_operational_error"],
    )
    def test_health_ready_db_failure(self, client: TestClient, exc: Exception):
        """/health/ready returns 503 when the DB is unavailable.

        Parametrized over both a plain ``RuntimeError`` and the real-world
        ``sqlalchemy.exc.OperationalError`` so the deliberately broad
        ``except Exception`` in ``health_ready`` is PROVEN broad. A single
        RuntimeError case would still pass against a handler narrowed to
        ``except RuntimeError`` — which would let a genuine driver failure
        escape as a 500, and a 500 is not a readiness signal k8s acts on the
        same way. Now that readiness actually probes this path (gitops #2553),
        the difference is the difference between a pod being pulled out of
        rotation and a pod serving auth against a dead database.
        """
        from models import DatabaseManager

        with patch.object(DatabaseManager, "get_session", side_effect=exc):
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["detail"] == "Database not ready"


class TestGetActiveLicense:
    """Test GET /licenses/active — JWT-mode entitlement lookup."""

    def test_requires_auth(self, client: TestClient):
        """Unauthenticated callers are rejected (401/403)."""
        response = client.get("/licenses/active?tenant_id=tenant-1&app_key=test-app")
        assert response.status_code in (401, 403)

    def test_active_license_found(self, auth_client_admin: TestClient, full_seed_data):
        """Active license for (tenant_id, app_key) resolves to introspect-shape 200."""
        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000001", "app_key": "test-app"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is True
        assert data["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert data["license_id"] == str(full_seed_data["license"].id)
        assert data["app"]["key"] == "test-app"

    def test_no_license_returns_404(self, auth_client_admin: TestClient, full_seed_data):
        """Unknown (tenant, app) returns 404 no_active_license."""
        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": "no-such-tenant", "app_key": "test-app"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "no_active_license"

    def test_missing_params(self, auth_client_admin: TestClient):
        """Missing required query params → 422 from FastAPI."""
        response = auth_client_admin.get("/licenses/active?tenant_id=tenant-1")
        assert response.status_code == 422

    def test_reader_role_allowed(self, auth_client_reader: TestClient, full_seed_data):
        """A least-privilege caller holding only `license-reader` may read active
        status — reading active-license status is a read operation (WS-2)."""
        response = auth_client_reader.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000001", "app_key": "test-app"},
        )
        assert response.status_code == 200
        assert response.json()["active"] is True

    def test_reader_role_denied_on_internal_listing(self, auth_client_reader: TestClient):
        """Scope guard: the `/licenses/active` relaxation must NOT leak into the
        broader /internal/licenses* listing surface, which stays internal-service
        only. A `license-reader`-only caller is still 403 there."""
        response = auth_client_reader.get("/internal/licenses")
        assert response.status_code == 403

    def test_reader_denied_cross_tenant(
        self, auth_client_reader: TestClient, full_seed_data, seed_other_tenant_license
    ):
        """Cross-tenant IDOR guard: a `license-reader` caller authenticated for
        tenant A must NOT be able to read tenant B's active license by passing
        ``?tenant_id=<B>``. It gets 403, and none of B's license_id / subject_id /
        plan / metadata is disclosed."""
        # Caller's authoritative tenant is the seed primary (X-Tenant-Id in the
        # reader client fixture); request a DIFFERENT tenant's entitlement.
        response = auth_client_reader.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000002", "app_key": "test-app"},
        )
        assert response.status_code == 403
        body = response.json()
        # No cross-tenant fields leaked in the error body.
        assert str(seed_other_tenant_license.id) not in str(body)

    def test_internal_service_allowed_cross_tenant(
        self, auth_client_admin: TestClient, full_seed_data, seed_other_tenant_license
    ):
        """An internal-service role (here `license-admin`) is the legitimate
        cross-tenant S2S entitlement-lookup surface and may resolve any tenant —
        the scope guard applies only to bare readers."""
        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000002", "app_key": "test-app"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is True
        assert data["tenant_id"] == "01000000-0000-7000-8000-000000000002"
        assert data["license_id"] == str(seed_other_tenant_license.id)


SEED_TENANT = "01000000-0000-7000-8000-000000000001"


def _mint_seat(
    db_manager,
    seed_app,
    seed_plan,
    *,
    keycloak_sub: str,
    expires_at,
    tenant_id: str = SEED_TENANT,
):
    """Mint one ACTIVE seat (subject + license) in (tenant, app).

    One row per onboarded user is what provisioning actually does; the partial
    unique index keys on ``subject_id`` so N seats per (tenant, app) is legal.
    """
    from uuid import UUID as _UUID

    subject = db_manager.create_or_get_subject(
        keycloak_sub=keycloak_sub, keycloak_realm="secure-apps"
    )
    lic = db_manager.create_license(
        subject_id=_UUID(str(subject.id)),
        app_id=_UUID(str(seed_app.id)),
        plan_id=_UUID(str(seed_plan.id)),
        tenant_id=tenant_id,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=expires_at,
    )
    return subject, lic


class TestGetActiveLicenseMultiSeat:
    """GET /licenses/active under the per-ORG licensing model.

    ``fr``/``kanban``/``n8n`` are sold as one package to a whole customer org,
    but ``License.subject_id`` is NOT NULL and the partial unique index is on
    ``(tenant_id, app_id, subject_id)`` — so provisioning mints one row per
    onboarded USER. Two employees in one org therefore means TWO active rows for
    the same (tenant, app). That is a normal, expected state, and the question
    "does this tenant hold an active license for this app" is answered *yes*.

    The old 409 ``multiple_active_licenses`` branch answered it with a conflict.
    Downstream, license-authz ``jwt_mode.py`` calls ``raise_for_status()`` and
    ``src/main.py`` maps the resulting ``httpx.HTTPError`` to a 503
    ``licenses_unavailable`` — so every two-employee tenant got a tenant-wide
    503 on ``api.nonagentic.ai``, reported as a licensing OUTAGE that was not
    happening. Observed live for ``nonagentic-gtm`` and ``n8n-dev``.
    """

    def test_two_seats_one_tenant_returns_200(
        self, auth_client_admin: TestClient, full_seed_data, seed_second_license
    ):
        """THE production outage: two subjects, one tenant, one app → 200, not 409.

        This is the single most important assertion in this module.
        """
        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": SEED_TENANT, "app_key": "test-app"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["active"] is True
        assert data["candidate_count"] == 2
        # Deterministic pick: seed_license expires +30d, the second seat +5d.
        assert data["license_id"] == str(full_seed_data["license"].id)

    def test_subject_id_as_keycloak_sub_selects_that_seat(
        self, auth_client_admin: TestClient, full_seed_data, seed_second_license
    ):
        """``subject_id`` carrying a Keycloak ``sub`` resolves to THAT seat.

        license-authz only ever holds the Keycloak ``sub`` — never the locally
        minted ``Subject.id`` — so this is the form the real caller will send.
        """
        response = auth_client_admin.get(
            "/licenses/active",
            params={
                "tenant_id": SEED_TENANT,
                "app_key": "test-app",
                "subject_id": "test-user-sub-2",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["license_id"] == str(seed_second_license.id)
        assert data["subject_match"] is True
        assert data["ambiguous"] is False

    def test_subject_id_as_subject_uuid_selects_same_seat(
        self,
        auth_client_admin: TestClient,
        full_seed_data,
        seed_second_subject,
        seed_second_license,
    ):
        """``subject_id`` carrying the local ``Subject.id`` UUID resolves identically.

        Pins the DUAL resolution: ``Subject.id`` and ``Subject.keycloak_sub`` are
        both UUID-shaped and indistinguishable by inspection, so resolving only
        one form would let every caller silently fall through to the tenant pick
        — the bug would look fixed and not be.
        """
        response = auth_client_admin.get(
            "/licenses/active",
            params={
                "tenant_id": SEED_TENANT,
                "app_key": "test-app",
                "subject_id": str(seed_second_subject.id),
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["license_id"] == str(seed_second_license.id)
        assert data["subject_match"] is True

    def test_pick_prefers_perpetual_and_is_stable_across_calls(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Three seats (+5d, +90d, perpetual) → the perpetual one wins, every call.

        ``expires_at DESC NULLS FIRST`` is the only ordering that is monotone
        under time; a created_at-first ordering would let the chosen license
        expire underneath the caller while a longer-lived sibling sat unchosen.
        """
        now = datetime.now(UTC)
        _, short = _mint_seat(
            db_manager,
            seed_app,
            seed_plan,
            keycloak_sub="seat-5d",
            expires_at=now + timedelta(days=5),
        )
        _, long = _mint_seat(
            db_manager,
            seed_app,
            seed_plan,
            keycloak_sub="seat-90d",
            expires_at=now + timedelta(days=90),
        )
        _, perpetual = _mint_seat(
            db_manager, seed_app, seed_plan, keycloak_sub="seat-perpetual", expires_at=None
        )

        ids = []
        for _ in range(2):
            response = auth_client_admin.get(
                "/licenses/active",
                params={"tenant_id": SEED_TENANT, "app_key": "test-app"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["candidate_count"] == 3
            ids.append(response.json()["license_id"])

        assert ids[0] == str(perpetual.id)
        assert ids[0] == ids[1], "pick must be stable across calls"
        assert str(short.id) not in ids and str(long.id) not in ids

    def test_tiebreak_is_min_id_when_expiry_and_created_at_are_identical(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Identical ``expires_at`` AND identical ``created_at`` → ``min(id)`` wins.

        ``created_at`` has a PYTHON-side default, so a seed script or a batched
        provision run really can produce byte-identical timestamps. Without the
        ``id ASC`` clause the ordering is not TOTAL and the pick falls back to
        whatever the plan happens to emit — which changes after any
        UPDATE/VACUUM/restore and differs between replicas. This is the clause a
        naive implementation drops.

        HONEST SCOPE — measured, not assumed: this test asserts the end-to-end
        CONTRACT but it does NOT discriminate the clause. The two rows here are
        rewritten in reverse id order so the surviving HEAP order disagrees with
        id order (verified: ctids (0,3)=hi, (0,4)=lo, and a bare
        ``ORDER BY expires_at DESC, created_at ASC`` on that heap really does
        return hi first) — yet with ``id ASC`` deleted the ORM query, whose plan
        sorts the output of a hash join rather than the raw heap, still returned
        ``min(id)`` and this test still PASSED. Undefined order is allowed to
        coincide with the right answer. The gate that genuinely fails when the
        clause is dropped is
        ``test_candidate_query_emits_the_total_order_by`` below; the two are
        deliberately kept as a pair.
        """
        now = datetime.now(UTC)
        _, first = _mint_seat(
            db_manager,
            seed_app,
            seed_plan,
            keycloak_sub="tie-a",
            expires_at=now + timedelta(days=7),
        )
        _, second = _mint_seat(
            db_manager,
            seed_app,
            seed_plan,
            keycloak_sub="tie-b",
            expires_at=now + timedelta(days=7),
        )
        lo, hi = sorted([UUID(str(first.id)), UUID(str(second.id))])

        # Force an exact tie on BOTH leading sort keys. Update the HIGHER id
        # first so the lower-id row ends up LAST in physical/heap order.
        with db_manager.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for lic_id in (hi, lo):
                conn.execute(
                    text(
                        "UPDATE license SET created_at = timestamptz '2020-01-01 00:00:00+00', "
                        "expires_at = timestamptz '2099-01-01 00:00:00+00' WHERE id = :i"
                    ),
                    {"i": str(lic_id)},
                )

        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": SEED_TENANT, "app_key": "test-app"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["license_id"] == str(lo)

    def test_candidate_query_emits_the_total_order_by(self, db_manager, seed_license):
        """The ORDER BY the candidate query ACTUALLY issues must be the full,
        total, three-key ordering — in that exact sequence.

        This is the discriminating gate for ``ACTIVE_LICENSE_PICK_ORDER``: it
        fails if any key is dropped, reordered, or if ``.order_by()`` is removed
        from the query altogether (i.e. the constant is defined but not applied).
        A behavioural test cannot do that job, because "undefined order" is free
        to coincide with the correct answer — see the tiebreak test above, which
        passed against a build with ``id ASC`` deleted.

        The clause is asserted against the SQL sent to postgres rather than
        against the constant alone, so "defined but never wired in" also fails.
        """
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db_manager.engine, "before_cursor_execute", _record)
        try:
            rows = db_manager.get_active_license(tenant_id=SEED_TENANT, app_key="test-app")
        finally:
            event.remove(db_manager.engine, "before_cursor_execute", _record)

        assert len(rows) == 1, "fixture precondition: exactly one seeded seat"
        selects = [s for s in statements if s.lstrip().lower().startswith("select")]
        assert len(selects) == 1, f"expected one candidate SELECT, got {selects}"
        assert (
            "ORDER BY license.expires_at DESC NULLS FIRST, "
            "license.created_at ASC, license.id ASC" in " ".join(selects[0].split())
        ), selects[0]

    def test_unknown_subject_id_still_returns_200_via_fallback(
        self, auth_client_admin: TestClient, full_seed_data, seed_second_license
    ):
        """DO-NOT-TIGHTEN guard: an unresolvable ``subject_id`` DEGRADES, never denies.

        A caller whose tenant holds a license but who holds none of its own must
        keep working exactly as today. This change removes a live 503; it is not
        the place to decide per-seat vs per-org admission.
        """
        for garbage in ("no-such-subject", str(uuid4()), "not-a-uuid-at-all", ""):
            response = auth_client_admin.get(
                "/licenses/active",
                params={
                    "tenant_id": SEED_TENANT,
                    "app_key": "test-app",
                    "subject_id": garbage,
                },
            )
            assert response.status_code == 200, f"{garbage!r} -> {response.text}"
            data = response.json()
            assert data["active"] is True
            assert data["license_id"] == str(full_seed_data["license"].id)

    def test_single_license_is_not_ambiguous(self, auth_client_admin: TestClient, full_seed_data):
        """One seat → not ambiguous, count 1, and no subject was asked for."""
        response = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": SEED_TENANT, "app_key": "test-app"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["candidate_count"] == 1
        assert data["ambiguous"] is False
        assert data["subject_match"] is None

    def test_ambiguous_only_when_we_had_to_guess(
        self,
        auth_client_admin: TestClient,
        full_seed_data,
        seed_second_license,
        caplog,
    ):
        """The WARN fires ONLY on the fallback path with >1 candidate.

        Warning merely because >1 license exists would page on SUCCESS — under
        the per-org model that is just a tenant with two employees. So the
        matched-subject leg must stay SILENT even though the candidate set is
        the same size.
        """
        with caplog.at_level(logging.WARNING, logger="main"):
            resolved = auth_client_admin.get(
                "/licenses/active",
                params={
                    "tenant_id": SEED_TENANT,
                    "app_key": "test-app",
                    "subject_id": "test-user-sub-2",
                },
            )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["ambiguous"] is False
        assert not [r for r in caplog.records if "ambiguous_active_license" in r.getMessage()]

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="main"):
            guessed = auth_client_admin.get(
                "/licenses/active",
                params={"tenant_id": SEED_TENANT, "app_key": "test-app"},
            )
        assert guessed.status_code == 200, guessed.text
        assert guessed.json()["ambiguous"] is True
        assert guessed.json()["candidate_count"] == 2
        assert [r for r in caplog.records if "ambiguous_active_license" in r.getMessage()]

    def test_subject_id_cannot_bypass_caller_scoping(
        self,
        auth_client_reader: TestClient,
        full_seed_data,
        seed_other_tenant_license,
    ):
        """PR #93's guard is untouched: ``subject_id`` is not a way around it.

        A bare ``license-reader`` may only resolve its OWN reconciled tenant. The
        new param narrows WITHIN an already-authorized (tenant, app) result set,
        so it can never widen the tenant scope.
        """
        response = auth_client_reader.get(
            "/licenses/active",
            params={
                "tenant_id": "01000000-0000-7000-8000-000000000002",
                "app_key": "test-app",
                "subject_id": "test-user-sub",
            },
        )
        assert response.status_code == 403
        assert str(seed_other_tenant_license.id) not in response.text

    def test_subject_id_from_another_tenant_does_not_leak(
        self,
        auth_client_reader: TestClient,
        full_seed_data,
        seed_second_license,
        db_manager,
        seed_app,
        seed_plan,
    ):
        """A subject that exists but holds no seat in the caller's tenant is a
        plain fallback — no cross-tenant row is reachable, and no existence
        oracle is created (unknown and known-elsewhere both read as no match)."""
        _mint_seat(
            db_manager,
            seed_app,
            seed_plan,
            keycloak_sub="elsewhere-sub",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            tenant_id="01000000-0000-7000-8000-000000000002",
        )
        response = auth_client_reader.get(
            "/licenses/active",
            params={
                "tenant_id": SEED_TENANT,
                "app_key": "test-app",
                "subject_id": "elsewhere-sub",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tenant_id"] == SEED_TENANT
        assert data["license_id"] == str(full_seed_data["license"].id)
        assert data["subject_match"] is False

    def test_openapi_no_longer_advertises_409(self):
        """The schema must not advertise a conflict this route can no longer emit."""
        import main

        spec = main.app.openapi()
        responses = spec["paths"]["/licenses/active"]["get"]["responses"]
        assert "409" not in responses
        assert "409" not in str(spec["paths"]["/licenses/active"]["get"].get("description", ""))


class TestIntrospectEndpoint:
    """Test /license/introspect endpoint."""

    def test_introspect_valid_token(self, client: TestClient, full_seed_data):
        """Introspect with a valid token returns active=True with full details."""
        raw_token = full_seed_data["raw_token"]
        response = client.post("/license/introspect", json={"token": raw_token})
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is True
        assert data["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert data["license_id"] == str(full_seed_data["license"].id)
        assert data["subject_id"] == str(full_seed_data["subject"].id)
        assert data["app"] is not None
        assert data["plan"] is not None
        assert data["status"] == "active"

    def test_introspect_invalid_token(self, client: TestClient):
        """Introspect with invalid token returns active=False with reason."""
        response = client.post("/license/introspect", json={"token": "invalid-token"})
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False

    def test_introspect_expired_token(self, client: TestClient, seed_expired_token):
        """Introspect with expired token returns active=False."""
        _, raw_token = seed_expired_token
        response = client.post("/license/introspect", json={"token": raw_token})
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False
        assert data["reason"] == "token_invalid"

    def test_introspect_revoked_license_token(
        self, client: TestClient, db_manager, seed_revoked_license
    ):
        """Introspect with token for revoked license returns active=False."""
        _, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_revoked_license.id)),
            token_type="api",
            expires_at=None,
        )
        response = client.post("/license/introspect", json={"token": raw_token})
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False
        assert data["reason"] == "license_inactive"

    def test_introspect_returns_none(self, client: TestClient):
        """Introspect when db_manager.introspect_token returns None covers line 533."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "introspect_token", return_value=None):
            response = client.post("/license/introspect", json={"token": "some-token"})
            assert response.status_code == 200
            data = response.json()
            assert data["active"] is False
            assert data["reason"] == "token_not_found"

    def test_introspect_db_error(self, client: TestClient):
        """Introspect returns a GENERIC 500 — never the raw exception text.

        /license/introspect is a PUBLIC, unauthenticated endpoint (the token is
        the auth), so its 500 body reaches anyone who can reach the service.
        The handler builds its own HTTPException, and the global sanitizing
        exception handler re-raises HTTPException unchanged, so whatever detail
        is set here is exactly what the caller sees.

        This assertion previously read ``assert "Introspection failed" in
        detail`` — which passed only because the handler interpolated
        ``str(e)``, i.e. the old test LOCKED IN the leak.
        """
        from models import DatabaseManager

        # Deliberately NOT a credential-shaped literal (a `password=` keyword here
        # trips the repo's detect-secrets gate). Host/role/dbname are enough to
        # prove raw DSN internals no longer reach the caller.
        internals = (
            "DB connection lost: host=postgres.example.com user=licenses_app dbname=licenses"
        )
        with patch.object(DatabaseManager, "introspect_token", side_effect=RuntimeError(internals)):
            response = client.post("/license/introspect", json={"token": "any-token"})
            assert response.status_code == 500
            detail = response.json()["detail"]
            # Generic, stable, caller-safe message.
            assert detail == "Internal server error"
            # And nothing from the underlying exception may appear.
            for leaked in ("DB connection lost", "postgres.example.com", "licenses_app", "dbname"):
                assert leaked not in detail

    def test_introspect_db_error_logs_the_real_cause(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        """The real error is still recorded server-side — sanitized, not swallowed."""
        from models import DatabaseManager

        with patch.object(
            DatabaseManager, "introspect_token", side_effect=RuntimeError("DB connection lost")
        ):
            with caplog.at_level(logging.ERROR):
                response = client.post("/license/introspect", json={"token": "any-token"})

        assert response.status_code == 500
        assert "DB connection lost" in caplog.text


class TestRevokeEndpoint:
    """Test /license/revoke endpoint."""

    def test_revoke_token(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can revoke a valid token."""
        response = auth_client_admin.post(
            "/license/revoke", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is True

    def test_revoke_invalid_token(self, auth_client_admin: TestClient):
        """Revoking an invalid token returns revoked=False."""
        response = auth_client_admin.post("/license/revoke", json={"token": "nonexistent-token"})
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is False

    def test_revoke_by_license_id(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can revoke all tokens for a license by license_id."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is True

    def test_revoke_nonexistent_license_id(self, auth_client_admin: TestClient):
        """Revoking by nonexistent license_id returns revoked=False."""
        response = auth_client_admin.post("/license/revoke", json={"license_id": str(uuid4())})
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is False

    def test_revoke_neither_token_nor_license_id(self, auth_client_admin: TestClient):
        """Revoke with neither token nor license_id returns 400."""
        response = auth_client_admin.post("/license/revoke", json={})
        assert response.status_code == 400

    def test_revoke_other_tenant_token(
        self, auth_client_admin: TestClient, db_manager, seed_other_tenant_license
    ):
        """Revoking a token from another tenant returns revoked=False."""
        # Create token for tenant-2 license
        _, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_other_tenant_license.id)),
            token_type="api",
            expires_at=None,
        )
        # auth_client_admin uses tenant-1
        response = auth_client_admin.post("/license/revoke", json={"token": raw_token})
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is False

    def test_revoke_other_tenant_license_id(
        self, auth_client_admin: TestClient, seed_other_tenant_license
    ):
        """Revoking license_id from another tenant returns revoked=False."""
        license_id = str(seed_other_tenant_license.id)
        response = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is False

    def test_revoke_invalid_license_id_format(self, auth_client_admin: TestClient):
        """Revoking with invalid UUID format returns 400."""
        response = auth_client_admin.post("/license/revoke", json={"license_id": "not-a-uuid"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid UUID"

    def test_revoke_token_reports_the_not_found_outcome(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """The ``not_found`` arm of the route's three-way outcome.

        Previously this patched ``revoke_token`` to return False and asserted the
        message ``"Token not found"`` — a distinct string reached only after a
        SUCCESSFUL introspect, i.e. a case that revealed the token existed. Both
        the two-call shape and that separate message are gone: ownership and the
        write now happen in one transaction (``revoke_token_for_tenant``), and
        "no such token" and "not your tenant" deliberately share one answer. See
        ``tests/unit/test_revoke_reaches_every_token_the_tenant_owns.py``.
        """
        from models import DatabaseManager

        raw_token = full_seed_data["raw_token"]
        with patch.object(
            DatabaseManager, "revoke_token_for_tenant", return_value=("not_found", 0)
        ):
            response = auth_client_admin.post("/license/revoke", json={"token": raw_token})
            assert response.status_code == 200
            data = response.json()
            assert data["revoked"] is False
            assert data["message"] == "Token not found or already invalid"

    def test_revoke_license_returns_false(self, auth_client_admin: TestClient, full_seed_data):
        """The revoke reports not_found (row vanished between read and write)."""
        from models import DatabaseManager

        license_id = str(full_seed_data["license"].id)
        with patch.object(
            DatabaseManager,
            "revoke_license_releasing_dependencies",
            return_value=("not_found", [], []),
        ):
            response = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
            assert response.status_code == 200
            data = response.json()
            assert data["revoked"] is False
            assert data["message"] == "License not found"

    def test_revoke_db_error(self, auth_client_admin: TestClient, full_seed_data):
        """A DB fault on the token arm becomes a sanitised 500, not a leak.

        Repointed from ``introspect_token`` to ``revoke_token_for_tenant``: the
        token arm no longer introspects, so patching the old target raised
        nothing and this test would have passed while exercising the happy path
        instead of the exception handler.
        """
        from models import DatabaseManager

        raw_token = full_seed_data["raw_token"]
        with patch.object(
            DatabaseManager, "revoke_token_for_tenant", side_effect=RuntimeError("DB crash")
        ):
            response = auth_client_admin.post("/license/revoke", json={"token": raw_token})
            assert response.status_code == 500
            assert response.json()["detail"] == "Revocation failed"
            # The sanitiser must not echo the cause (a SQLAlchemy error
            # stringifies to the statement plus its bound parameters).
            assert "DB crash" not in response.text


class TestLicenseEndpoints:
    """Test license CRUD endpoints."""

    def test_list_licenses(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can list licenses filtered by tenant."""
        response = auth_client_admin.get("/licenses")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        license_ids = [lic["id"] for lic in data]
        assert str(full_seed_data["license"].id) in license_ids
        found = next(lic for lic in data if lic["id"] == str(full_seed_data["license"].id))
        assert found["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert found["status"] == "active"
        assert found["app_id"] == str(full_seed_data["app"].id)

    def test_list_licenses_with_filters(self, auth_client_admin: TestClient, full_seed_data):
        """List licenses with status filter."""
        response = auth_client_admin.get("/licenses?status=active")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for lic in data:
            assert lic["status"] == "active"

    def test_list_licenses_invalid_uuid_filter(self, auth_client_admin: TestClient):
        """List licenses with invalid UUID filter returns 400."""
        response = auth_client_admin.get("/licenses?app_id=not-a-uuid")
        assert response.status_code == 400

    def test_list_all_licenses_admin_returns_cross_tenant(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """Admin endpoint returns licenses regardless of X-Tenant-Id header.

        Used by portal-bff's /api/licenses/admin/all to surface every tenant's
        licenses to the platform-admin user. Unlike GET /licenses, this route
        deliberately does NOT depend on get_tenant_id.
        """
        # Send a tenant header that DOES NOT own the seeded license to prove
        # the admin endpoint ignores the header rather than filtering by it.
        unrelated_tenant = "01000000-0000-7000-8000-00000000ffff"
        response = auth_client_admin.get(
            "/licenses/admin/all", headers={"X-Tenant-Id": unrelated_tenant}
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        license_ids = [lic["id"] for lic in data]
        assert str(full_seed_data["license"].id) in license_ids

    def test_list_all_licenses_admin_requires_admin_role(self, auth_client_reader: TestClient):
        """license-reader is insufficient for the admin endpoint."""
        response = auth_client_reader.get("/licenses/admin/all")
        assert response.status_code == 403

    def test_list_user_licenses(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can list licenses for a specific user."""
        subject = full_seed_data["subject"]
        response = auth_client_admin.get(f"/licenses/user/{subject.keycloak_sub}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["subject_id"] == str(subject.id)

    def test_list_user_licenses_not_found(self, auth_client_admin: TestClient):
        """List licenses for nonexistent user returns 404."""
        response = auth_client_admin.get("/licenses/user/nonexistent-user-sub")
        assert response.status_code == 404

    def test_create_user_license(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """Admin can create a license for a user."""
        response = auth_client_admin.post(
            "/licenses/user/new-user-sub",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "active"
        assert data["app_id"] == str(seed_app.id)
        assert data["plan_id"] == str(seed_plan.id)
        assert data["tenant_id"] == "01000000-0000-7000-8000-000000000001"

    def test_create_user_license_is_idempotent(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        """Re-provisioning the same (tenant, app, subject) returns the existing
        active license instead of minting a duplicate (portal-bff calls this on
        every session)."""
        body = {"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)}
        first = auth_client_admin.post("/licenses/user/dup-user-sub", json=body)
        assert first.status_code == 201
        second = auth_client_admin.post("/licenses/user/dup-user-sub", json=body)
        assert second.status_code == 201
        # Same license, not a new one.
        assert second.json()["id"] == first.json()["id"]
        # Exactly one active license for this subject.
        listing = auth_client_admin.get("/licenses/user/dup-user-sub")
        actives = [lic for lic in listing.json() if lic["status"] == "active"]
        assert len(actives) == 1

    def test_create_user_license_survives_a_concurrent_first_login(
        self, auth_client_admin: TestClient, db_manager, db_engine, seed_app, seed_plan
    ):
        """A brand-new user's FIRST login must not 500 when two bootstraps race.

        Drives the real mint path end-to-end. ``create_or_get_subject`` runs
        first in this handler and is the only thing that COMMITs before the
        license insert, so the seam below lands exactly on the subject race:
        an independent connection (the other replica) commits the same
        keycloak_sub between our SELECT-miss and our INSERT.

        Before the fix this returned 500 with the raw
        ``psycopg.errors.UniqueViolation`` text interpolated into ``detail``
        (the handler's ``except Exception`` does ``f"...: {str(e)}"``), i.e. an
        internal DB error string leaked to the caller on a user's first login.
        """
        keycloak_sub = "race-http-first-login"
        real_get_session = db_manager.get_session
        raced = {"done": False}

        def racing_get_session():
            session = real_get_session()
            real_commit = session.commit

            def commit_after_a_competitor_wins():
                if not raced["done"]:
                    raced["done"] = True
                    with db_engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as conn:
                        conn.execute(
                            text(
                                "INSERT INTO subject "
                                "(id, keycloak_sub, keycloak_realm, created_at) "
                                "VALUES (:id, :sub, :realm, now())"
                            ),
                            {
                                "id": str(uuid4()),
                                "sub": keycloak_sub,
                                "realm": "secure-apps",
                            },
                        )
                return real_commit()

            session.commit = commit_after_a_competitor_wins
            return session

        with patch.object(db_manager, "get_session", racing_get_session):
            response = auth_client_admin.post(
                f"/licenses/user/{keycloak_sub}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
            )

        assert raced["done"] is True, "seam never fired — the race was not simulated"
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "active"
        # Belt and braces: no internal DB error text reached the caller.
        assert "UniqueViolation" not in response.text
        assert "duplicate key" not in response.text

    def test_expired_license_is_renewable_not_deadlocked(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """RENEWAL DEADLOCK regression (real production victim: tenant
        subhankar-kag, app `kag`, expired 2026-07-18 and unrenewable).

        Nothing in the estate transitions a license out of ``status='active'``
        when ``expires_at`` passes. Such a row is simultaneously invisible to
        ``/licenses/active`` (which filters the time window) and still occupying
        the ``uq_license_active_tenant_app_subject`` slot — AND on main it is
        handed straight back by ``create_user_license``'s idempotency branch.
        The tenant is told it has no license and re-provisioning returns the
        corpse.

        Re-provisioning must instead retire the dead row and mint a fresh one.
        """
        from datetime import UTC, datetime, timedelta

        from models import License

        body = {"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)}
        first = auth_client_admin.post("/licenses/user/expired-renewal-sub", json=body)
        assert first.status_code == 201
        dead_id = first.json()["id"]

        # Age the license out WITHOUT transitioning its status — exactly the
        # production state (no CronJob/scheduler/sweep ever does that transition).
        session = db_manager.get_session()
        try:
            session.query(License).filter(License.id == UUID(dead_id)).update(
                {
                    "starts_at": datetime.now(UTC) - timedelta(days=400),
                    "expires_at": datetime.now(UTC) - timedelta(days=1),
                }
            )
            session.commit()
        finally:
            session.close()

        # Pre-condition: the tenant genuinely cannot see it any more.
        active_lookup = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000001", "app_key": seed_app.key},
        )
        assert active_lookup.status_code == 404

        # The renewal attempt.
        second = auth_client_admin.post("/licenses/user/expired-renewal-sub", json=body)
        assert second.status_code == 201
        new_id = second.json()["id"]

        # A NEW license was minted — main returns `dead_id` here (idempotent branch).
        assert new_id != dead_id
        assert second.json()["status"] == "active"

        # The corpse was retired, not left squatting on the unique-index slot.
        assert db_manager.get_license(UUID(dead_id)).status == "expired"

        # The partial unique index still holds: exactly ONE active row for the key.
        listing = auth_client_admin.get("/licenses/user/expired-renewal-sub")
        actives = [lic for lic in listing.json() if lic["status"] == "active"]
        assert len(actives) == 1
        assert actives[0]["id"] == new_id

        # And the tenant can see its license again.
        recheck = auth_client_admin.get(
            "/licenses/active",
            params={"tenant_id": "01000000-0000-7000-8000-000000000001", "app_key": seed_app.key},
        )
        assert recheck.status_code == 200
        assert recheck.json()["license_id"] == new_id

    def test_create_user_license_with_dates(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        """Create a license with explicit start/expire dates."""
        response = auth_client_admin.post(
            "/licenses/user/dated-user-sub",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
                "starts_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2027-01-01T00:00:00+00:00",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "2026-01-01" in data["starts_at"]
        assert "2027-01-01" in data["expires_at"]

    def test_create_user_license_with_metadata(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        """Create a license with metadata."""
        response = auth_client_admin.post(
            "/licenses/user/meta-user-sub",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
                "metadata": {"tier": "premium"},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["metadata"] == {"tier": "premium"}

    def test_create_user_license_invalid_app_id(self, auth_client_admin: TestClient, seed_plan):
        """Create license with invalid app_id returns 422 (Pydantic UUID validation)."""
        response = auth_client_admin.post(
            "/licenses/user/bad-user-sub",
            json={
                "app_id": "not-a-uuid",
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 422

    def test_list_user_licenses_db_error(self, auth_client_admin: TestClient, full_seed_data):
        """Covers lines 666-668: exception handler in list_user_licenses."""
        from models import DatabaseManager

        subject = full_seed_data["subject"]
        with patch.object(
            DatabaseManager, "list_licenses", side_effect=RuntimeError("DB unavailable")
        ):
            response = auth_client_admin.get(f"/licenses/user/{subject.keycloak_sub}")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list licenses"

    def test_create_user_license_db_error(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """Covers lines 792-794: exception handler in create_user_license."""
        from models import DatabaseManager

        with patch.object(
            DatabaseManager, "create_or_get_subject", side_effect=RuntimeError("DB write failed")
        ):
            response = auth_client_admin.post(
                "/licenses/user/error-user",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
            )
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to create license"

    def test_list_licenses_db_error(self, auth_client_admin: TestClient):
        """Covers lines 835-837: exception handler in list_licenses."""
        from models import DatabaseManager

        with patch.object(
            DatabaseManager, "list_licenses", side_effect=RuntimeError("DB query failed")
        ):
            response = auth_client_admin.get("/licenses")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list licenses"


class TestCreateUserLicenseMintGuard:
    """Mint guard on POST /licenses/user/{subject_id}: a license may only be
    minted for a REGISTERED tenant (Stage 1 of the license.tenant_id ->
    tenants.id FK campaign; pre-enforces the FK at the app layer).

    #73 made the create path use the strict ``require_tenant_id`` so a bare
    token subject can no longer arrive via the JWT-subject fallback. But the
    HEADER path is still open: a Keycloak ``sub`` (user id) IS a UUID, and
    portal-bff still forwards ``sub`` as ``X-Tenant-Id`` when the token carries
    no ``tenant_id`` claim. ``require_tenant_id`` happily trusts any UUID
    header, so the handler could STILL persist a user id as ``tenant_id`` (into
    ``licenses.tenant_id`` and the provisioned ``token_budgets.tenant_id`` in
    metering) — corrupting billing.

    The guard now refuses whenever ``get_tenant(tenant_id) is None`` — i.e. the
    resolved ``tenant_id`` is not a registered tenant — regardless of whether it
    equals the subject. This closes BOTH orphan sources: the money-bug (a user id
    sent as tenant) AND an unprovisioned-but-real tenant UUID. A tenant with a
    real ``tenants`` row is NOT rejected.
    """

    # A UUID with NO tenants row — exactly the shape of a Keycloak sub that a
    # misconfigured producer forwarded as X-Tenant-Id.
    _ORPHAN_SUBJECT = "019e00aa-0000-7000-8000-0000000000ab"
    # A legit SELF-TENANT: an id that is the subject AND has a tenants row. The
    # guard must NOT reject it. Synthetic since migration 046: this used to be the
    # live `e148e8af-…` portal-render-e2e id, which is no longer a tenant at all.
    _SELF_TENANT = "6a0f2c94-7d3b-4e15-b8a2-1c9e5f7d3b60"
    # portal-render-e2e after migration 046. USER sense: the keycloak_sub of the
    # service-account subject (unchanged). TENANT sense: the re-keyed uuid7.
    _RENDER_E2E_SUBJECT_SUB = "e148e8af-3751-49bf-a0fa-56ab46d31ddc"
    _RENDER_E2E_TENANT = "01a1113b-674f-72b0-a84b-dc746d39afca"

    def test_guard_refuses_when_tenant_equals_subject_and_no_tenants_row(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """tenant_id == subject_id AND get_tenant() is None -> 400, and NEITHER
        the license row NOR the metering budget is provisioned."""
        subject = self._ORPHAN_SUBJECT
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                # Producer forwards the user's sub as the tenant header.
                headers={"X-Tenant-Id": subject},
            )
        assert response.status_code == 400
        assert "refusing to mint" in response.json()["detail"]
        # Money-bug invariant: no license persisted, no budget provisioned.
        spy_create_license.assert_not_called()
        spy_provision.assert_not_called()

    def test_guard_fires_on_uuid_case_variant(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Same UUID differing only in CASE between the path subject and the
        header tenant must still trip the guard — the comparison canonicalizes
        both as UUIDs, not raw strings (a naive str== would have let this mint)."""
        subject_upper = self._ORPHAN_SUBJECT.upper()  # raw path param, uppercase
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject_upper}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                # tenant header = canonical lowercase form of the same UUID.
                headers={"X-Tenant-Id": self._ORPHAN_SUBJECT},
            )
        assert response.status_code == 400
        # Refused by whichever guard sees it first: since 2026-09-27 an upper-case
        # UUID that is not a subject row id is refused at subject creation
        # (``create_or_get_subject``: a case variant would be a second subject for one
        # person), which runs before the mint guard. Either refusal is a 400 with no
        # licence minted and no budget provisioned — the property this test pins.
        detail = response.json()["detail"]
        assert "refusing to mint" in detail or "must be lower case" in detail, detail
        spy_create_license.assert_not_called()
        spy_provision.assert_not_called()

    def test_guard_refuses_unregistered_tenant_distinct_from_subject(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Broadened guard: a valid-UUID tenant that is NOT the subject and has
        NO tenants row is now refused too (previously it would have minted an
        orphan license). This is the unprovisioned-tenant orphan source."""
        subject = "019e00aa-0000-7000-8000-0000000000cd"
        # A different, unregistered tenant UUID (not equal to the subject).
        unregistered_tenant = "019e00bb-0000-7000-8000-0000000000ef"
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": unregistered_tenant},
            )
        assert response.status_code == 400
        assert "refusing to mint" in response.json()["detail"]
        spy_create_license.assert_not_called()
        spy_provision.assert_not_called()

    def test_guard_passes_for_registered_tenant_distinct_from_subject(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Positive control for the broadened guard: a REGISTERED tenant that
        differs from the subject mints normally (the ordinary happy path)."""
        subject = "019e00aa-0000-7000-8000-0000000000dd"
        registered_tenant = "019e00cc-0000-7000-8000-0000000000aa"
        db_manager.ensure_tenant(registered_tenant, name="acme-registered", owner_key="platform")
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": registered_tenant},
            )
        assert response.status_code == 201
        assert response.json()["tenant_id"] == registered_tenant
        spy_create_license.assert_called_once()
        spy_provision.assert_called_once()

    def test_guard_does_not_fire_for_real_self_tenant(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Negative control: tenant_id == subject_id but a real tenants row
        EXISTS -> license is created normally.

        Gating on 'no tenants row' (not merely tenant==subject) keeps the legit
        self-tenant working."""
        # Ensure the real tenants row. This USED to stand in for migration 013,
        # which never ran on a fresh database — the runner baselines it as applied.
        # `seed_platform_tenants()` now creates the same row during bootstrap, so a
        # bare INSERT here hits tenants_pkey. Stated with the idempotent primitive
        # instead of deleted: the precondition this test's name claims is "a real
        # tenants row EXISTS", and it should assert that itself rather than silently
        # inherit it from bootstrap ordering.
        db_manager.ensure_tenant(
            self._SELF_TENANT,
            "self-tenant-org",
            keycloak_realm="portal-realm",
            owner_key="platform",
        )
        assert db_manager.get_tenant(self._SELF_TENANT) is not None

        subject = self._SELF_TENANT
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": subject},
            )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "active"
        assert data["tenant_id"] == self._SELF_TENANT
        # The guard did NOT fire: the license was minted and budget provisioned.
        spy_create_license.assert_called_once()
        spy_provision.assert_called_once()

    def _rekeyed_render_e2e(self, db_manager):
        """The licences-DB state migration 046 leaves: the tenant on its uuid7, the
        service-account subject still carrying the OLD id as its keycloak_sub, and
        NO tenants row on the old id. Returns the subject's primary key."""
        db_manager.ensure_tenant(
            self._RENDER_E2E_TENANT,
            "portal-render-e2e",
            keycloak_realm="portal-realm",
            owner_key="platform",
        )
        tenant = db_manager.get_tenant("portal-render-e2e")
        assert tenant is not None and str(tenant.id) == self._RENDER_E2E_TENANT
        subject = db_manager.create_or_get_subject(
            keycloak_sub=self._RENDER_E2E_SUBJECT_SUB, keycloak_realm="portal"
        )
        assert db_manager.get_tenant(self._RENDER_E2E_SUBJECT_SUB) is None
        return str(subject.id)

    def test_the_rekeyed_render_e2e_subject_mints_for_the_new_tenant(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """After 046 the service-account subject (keycloak_sub = the OLD id) mints
        for a tenant that is no longer itself. That is the ordinary
        registered-tenant path — `self_tenant_mint` is False and nothing needs
        exempting — and it must keep working: 201, attributed to the NEW tenant,
        held by the SAME subject row, and idempotent on the second call."""
        subject_pk = self._rekeyed_render_e2e(db_manager)
        with patch("main._provision_budget", new_callable=AsyncMock) as spy_provision:
            first = auth_client_admin.post(
                f"/licenses/user/{self._RENDER_E2E_SUBJECT_SUB}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": self._RENDER_E2E_TENANT},
            )
            again = auth_client_admin.post(
                f"/licenses/user/{self._RENDER_E2E_SUBJECT_SUB}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": self._RENDER_E2E_TENANT},
            )
        assert first.status_code == 201, first.text
        assert first.json()["tenant_id"] == self._RENDER_E2E_TENANT
        assert first.json()["subject_id"] == subject_pk
        spy_provision.assert_called_once()
        assert again.status_code == 201, again.text
        assert again.json()["id"] == first.json()["id"], "second call must be idempotent"
        # The principal kept its sub; no second subject was minted for it.
        kept = db_manager.get_subject_by_keycloak_sub(self._RENDER_E2E_SUBJECT_SUB)
        assert kept is not None and str(kept.id) == subject_pk

    def test_the_rekeyed_render_e2e_old_id_is_no_longer_a_mintable_tenant(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """The old id is not special any more: sent as `X-Tenant-Id` — on the
        self-tenant shape that used to be pinned at 201 — it is refused by the mint
        guard like any unregistered id, and it cannot be registered back, because it
        is a subject's keycloak_sub."""
        from models import ClientSafeError

        self._rekeyed_render_e2e(db_manager)
        with (
            patch.object(
                db_manager, "create_license", wraps=db_manager.create_license
            ) as spy_create_license,
            patch("main._provision_budget", new_callable=AsyncMock) as spy_provision,
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{self._RENDER_E2E_SUBJECT_SUB}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": self._RENDER_E2E_SUBJECT_SUB},
            )
        assert response.status_code == 400, response.text
        assert "refusing to mint" in response.json()["detail"]
        spy_create_license.assert_not_called()
        spy_provision.assert_not_called()
        with pytest.raises(ClientSafeError):
            db_manager.ensure_tenant(
                self._RENDER_E2E_SUBJECT_SUB, "portal-render-e2e-reborn", owner_key="platform"
            )
        assert db_manager.get_tenant(self._RENDER_E2E_SUBJECT_SUB) is None


class TestInternalLicenseEndpoints:
    """GET /internal/licenses[/{id}] for internal service consumers (metering,
    billing, onboarding). Regression for the migration-016 retype: these build
    their response dict INLINE (not via License.to_dict), so tenant_id (a UUID
    column now) must be str()'d — else Pydantic rejects the raw UUID for the
    `str` field and every real row 400s. license-admin satisfies the internal
    role dependency."""

    def test_internal_list_returns_string_tenant_id(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """A real license lists as 200 with a STRING tenant_id (not a 400 from a
        raw UUID hitting InternalLicenseResponse.tenant_id: str)."""
        resp = auth_client_admin.get("/internal/licenses")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert isinstance(rows, list) and len(rows) >= 1
        row = next(r for r in rows if r["id"] == str(full_seed_data["license"].id))
        assert row["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert isinstance(row["tenant_id"], str)

    def test_internal_get_returns_string_tenant_id(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """A single real license fetches as 200 with a STRING tenant_id."""
        lid = str(full_seed_data["license"].id)
        resp = auth_client_admin.get(f"/internal/licenses/{lid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == "01000000-0000-7000-8000-000000000001"

    def test_internal_list_non_uuid_tenant_filter_returns_empty(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """A non-UUID tenant_id query param resolves to no-match (empty 200), not
        a 500 from InvalidTextRepresentation against the UUID column."""
        resp = auth_client_admin.get("/internal/licenses", params={"tenant_id": "not-a-uuid"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_internal_list_uuid_tenant_filter_matches(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """A valid UUID tenant filter returns that tenant's licenses."""
        resp = auth_client_admin.get(
            "/internal/licenses",
            params={"tenant_id": "01000000-0000-7000-8000-000000000001"},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) >= 1

    def test_internal_licences_publish_the_per_licence_overrides(
        self, auth_client_admin: TestClient, db_manager, full_seed_data, seed_second_license
    ):
        """The licence's own token_budget_override / quota_override (migration 041)
        reach internal consumers: billing's sync-from-licenses set a licence's budget
        from the PLAN because these were not exposed (prod: override 100,000, billing
        held 50,000,000). A licence with an override publishes it; one without
        publishes None — never the plan's value under the override's name."""
        with_override = str(full_seed_data["license"].id)
        without_override = str(seed_second_license.id)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE license SET token_budget_override = 100000, quota_override = 0 "
                    "WHERE id = :i"
                ),
                {"i": with_override},
            )

        resp = auth_client_admin.get("/internal/licenses")
        assert resp.status_code == 200, resp.text
        rows = {r["id"]: r for r in resp.json()}
        assert rows[with_override]["token_budget_override"] == 100000
        # 0 is BLOCK-ALL, a value — it must not collapse into "unset".
        assert rows[with_override]["quota_override"] == 0
        assert rows[without_override]["token_budget_override"] is None
        assert rows[without_override]["quota_override"] is None

        one = auth_client_admin.get(f"/internal/licenses/{with_override}")
        assert one.status_code == 200, one.text
        assert one.json()["token_budget_override"] == 100000
        assert one.json()["quota_override"] == 0
        other = auth_client_admin.get(f"/internal/licenses/{without_override}")
        assert other.status_code == 200, other.text
        assert other.json()["token_budget_override"] is None
        assert other.json()["quota_override"] is None


class TestInternalTenantsEndpoint:
    """GET /internal/tenants — the uuid -> human-name identity map billing reads
    to render its per-tenant cost ledger. `billing_cost_daily.tenant_name` holds
    the raw uuid for almost every tenant because the emitters only see the uuid,
    and billing has its own database, so the name must come over HTTP.

    The conftest reset registers exactly two tenants (`seed-tenant-primary` and
    `seed-tenant-other`), so these assert on the presence/absence of NAMED rows
    rather than on a total, which would break the moment a fixture seeds another.
    license-admin satisfies the internal role dependency."""

    def test_service_actors_are_always_returned_here(
        self, auth_client_admin: TestClient, db_manager
    ):
        """The regression guard for this endpoint's whole purpose.

        `list_tenants` excludes service actors by default because it backs an
        operator PICK-LIST. This endpoint is an IDENTITY MAP: billing calls it to
        put a human name on rows it has ALREADY billed, and service actors emit
        real on-behalf spend (33 of 59 live prod rows). If this handler ever
        inherits the pick-list default, those tenants become unnameable in the
        cost ledger and every one of their rows renders as a raw uuid — the exact
        failure the endpoint was built to prevent. It fails silently, with a 200.
        """
        db_manager.ensure_tenant(
            uuid4(), name="svc-ledger-nameable", kind="service", owner_key="platform"
        )

        resp = auth_client_admin.get("/internal/tenants")
        assert resp.status_code == 200, resp.text
        names = {r["name"] for r in resp.json()}
        assert "svc-ledger-nameable" in names

    def test_exposes_kind_and_cost_centre_for_billing(
        self, auth_client_admin: TestClient, db_manager
    ):
        """Billing cannot reach GET /tenants — its service account is refused
        with 403 — so this is the only route by which `kind` reaches the ledger.
        Without it, cost-by-tenant-kind needs a crosswalk maintained by hand in
        billing_service/src/tenancy.py, which is how the registry and the ledger
        drifted apart before."""
        db_manager.ensure_tenant(uuid4(), name="svc-kinded", kind="service", owner_key="platform")

        resp = auth_client_admin.get("/internal/tenants")
        assert resp.status_code == 200, resp.text
        by_name = {r["name"]: r for r in resp.json()}
        assert by_name["svc-kinded"]["kind"] == "service"
        # Declared on every row, not only the ones that happen to carry a value.
        assert all("kind" in r and "cost_centre" in r for r in resp.json())

    def test_returns_tenant_names_not_uuids(self, auth_client_admin: TestClient, db_manager):
        """200 with a STRING id and the human name — the whole point of the
        endpoint. A raw UUID here would 400 against `InternalTenantResponse.id: str`."""
        db_manager.create_tenant(
            name="ledger-visible", keycloak_realm="portal-realm", owner_key="platform"
        )

        resp = auth_client_admin.get("/internal/tenants")
        assert resp.status_code == 200, resp.text
        rows = resp.json()

        by_name = {r["name"]: r for r in rows}
        assert "ledger-visible" in by_name
        row = by_name["ledger-visible"]
        assert isinstance(row["id"], str)
        assert UUID(row["id"])  # a real uuid, not a repr
        assert row["keycloak_realm"] == "portal-realm"
        assert row["archived"] is False
        # The two conftest-registered tenants are the ones billing's uuid-only
        # rows actually point at, so they must be nameable too.
        assert {"seed-tenant-primary", "seed-tenant-other"} <= set(by_name)
        assert all(r["archived"] is False for r in rows)

    def test_archived_tenant_is_excluded_by_default(
        self, auth_client_admin: TestClient, db_manager
    ):
        """A retired tenant must not appear in the default listing."""
        db_manager.create_tenant(name="retired-tenant", owner_key="platform")
        db_manager.set_tenant_archived("retired-tenant", True)

        resp = auth_client_admin.get("/internal/tenants")
        assert resp.status_code == 200, resp.text
        assert "retired-tenant" not in {r["name"] for r in resp.json()}

    def test_archived_tenant_is_returned_with_include_archived(
        self, auth_client_admin: TestClient, db_manager
    ):
        """...but a ledger backfill over an old window still has to name it, so
        `include_archived=true` returns it, flagged `archived: true`."""
        db_manager.create_tenant(name="retired-tenant", owner_key="platform")
        db_manager.set_tenant_archived("retired-tenant", True)

        resp = auth_client_admin.get("/internal/tenants", params={"include_archived": "true"})
        assert resp.status_code == 200, resp.text
        by_name = {r["name"]: r for r in resp.json()}
        assert by_name["retired-tenant"]["archived"] is True
        # include_archived widens the set, it does not replace it: the live
        # tenants are still there. Without this a handler that returned ONLY the
        # archived rows would pass the assertion above.
        assert by_name["seed-tenant-primary"]["archived"] is False

    def test_non_internal_caller_is_denied(self, auth_client_reader: TestClient, db_manager):
        """The tenant identity map is the whole customer list; a `license-reader`
        caller holds no internal-service role and is 403 — same boundary as
        /internal/licenses. And no tenant name leaks in the error body."""
        db_manager.create_tenant(name="must-not-leak", owner_key="platform")

        resp = auth_client_reader.get("/internal/tenants")
        assert resp.status_code == 403
        assert "must-not-leak" not in resp.text


class TestInternalAppsEndpoint:
    """GET /internal/apps — the app uuid <-> key identity map billing mirrors to
    resolve the ``X-App-Id`` key a caller sends. It must list REGISTERED apps,
    licensed or not: a platform app with no licence (``core-app``) is invisible
    to ``/internal/licenses``, which is the defect this endpoint exists to fix."""

    @staticmethod
    def _revoke(db_manager, app_id) -> None:
        session = db_manager.get_session()
        try:
            row = session.get(App, app_id)
            row.revoked = True
            session.commit()
        finally:
            session.close()

    def test_unlicensed_app_is_listed_with_id_and_key(
        self, auth_client_admin: TestClient, db_manager
    ):
        """The regression guard: an app with NO licence is in the map."""
        app = db_manager.create_app(
            key="platform-no-licence", name="Platform, no licence", issuers=("platform",)
        )

        resp = auth_client_admin.get("/internal/apps")
        assert resp.status_code == 200, resp.text
        by_key = {r["key"]: r for r in resp.json()}
        row = by_key["platform-no-licence"]
        assert row["id"] == str(app.id)
        assert UUID(row["id"])
        assert row["revoked"] is False
        assert row["audience"] in {"human", "platform", "automation"}

    def test_revoked_app_excluded_by_default(self, auth_client_admin: TestClient, db_manager):
        app = db_manager.create_app(key="retired-app", name="Retired", issuers=("platform",))
        self._revoke(db_manager, app.id)

        resp = auth_client_admin.get("/internal/apps")
        assert resp.status_code == 200, resp.text
        assert "retired-app" not in {r["key"] for r in resp.json()}

    def test_revoked_app_returned_flagged_with_include_revoked(
        self, auth_client_admin: TestClient, db_manager
    ):
        """A retired app still owns historical spend, so the identity map must be
        able to name it — flagged, and WITHOUT dropping the live apps."""
        db_manager.create_app(key="live-app", name="Live", issuers=("platform",))
        retired = db_manager.create_app(key="retired-app", name="Retired", issuers=("platform",))
        self._revoke(db_manager, retired.id)

        resp = auth_client_admin.get("/internal/apps", params={"include_revoked": "true"})
        assert resp.status_code == 200, resp.text
        by_key = {r["key"]: r for r in resp.json()}
        assert by_key["retired-app"]["revoked"] is True
        assert by_key["live-app"]["revoked"] is False

    def test_non_internal_caller_is_denied(self, auth_client_reader: TestClient, db_manager):
        """Same boundary as /internal/tenants: license-reader holds no internal role."""
        db_manager.create_app(key="must-not-leak-app", name="x", issuers=("platform",))

        resp = auth_client_reader.get("/internal/apps")
        assert resp.status_code == 403
        assert "must-not-leak-app" not in resp.text


class TestTokenCreateEndpoint:
    """Test /licenses/{license_id}/tokens endpoint."""

    def test_create_token_for_license(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can create a token for a license."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 201
        data = response.json()
        assert "token" in data
        assert data["token_type"] == "api"
        assert data["license_id"] == license_id

    def test_create_token_with_type(self, auth_client_admin: TestClient, full_seed_data):
        """Create a token with specific type."""
        license_id = str(full_seed_data["license"].id)
        # A node token needs an explicit expiry (or a long-lived registration) since
        # licenses#244; without one it is refused, see test_token_expiry_default.py.
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens?token_type=node&expires_days=30"
        )
        assert response.status_code == 201
        data = response.json()
        assert data["token_type"] == "node"

    def test_create_token_with_expiry(self, auth_client_admin: TestClient, full_seed_data):
        """Create a token with expiry days."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens?expires_days=30")
        assert response.status_code == 201
        data = response.json()
        assert data["expires_at"] is not None
        # Verify expires_at is approximately 30 days from now
        from datetime import UTC, datetime, timedelta

        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expected = datetime.now(UTC) + timedelta(days=30)
        # Allow 60 seconds tolerance for test execution time
        assert abs((expires_at - expected).total_seconds()) < 60

    # --- expires_days boundary: 0 must NOT mint a perpetual token -------------
    # `if expires_days:` was a truthiness test, so expires_days=0 took the same
    # branch as None and minted a NEVER-EXPIRING bearer credential behind a 201
    # whose `expires_at: null` was indistinguishable from the documented
    # "perpetual" case. Same 0-is-falsy class as the quota_monthly guards.

    def test_create_token_expires_days_zero_is_rejected(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        """expires_days=0 is invalid input (422) — it must never mint a perpetual token."""
        from models import Token

        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens?expires_days=0")
        assert response.status_code == 422, response.text
        # And nothing was minted: only the fixture's own token exists, and no
        # token on this license has a NULL (perpetual) expiry.
        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == UUID(license_id)).all()
            assert len(tokens) == 1
            assert [t.expires_at for t in tokens if t.expires_at is None] == []
        finally:
            session.close()

    def test_create_token_expires_days_negative_is_rejected(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """expires_days=-1 is invalid input (422)."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens?expires_days=-1")
        assert response.status_code == 422, response.text

    def test_create_token_expires_days_one_is_the_minimum(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """expires_days=1 — the smallest accepted lifetime — expires ~1 day out."""
        from datetime import UTC, datetime, timedelta

        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens?expires_days=1")
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["expires_at"] is not None
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expected = datetime.now(UTC) + timedelta(days=1)
        assert abs((expires_at - expected).total_seconds()) < 60
        # It is genuinely in the future — not the "expires immediately" reading of 0.
        assert expires_at > datetime.now(UTC)

    def test_create_token_expires_days_omitted_gets_the_bounded_default(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """Omitting expires_days on an api token is NO LONGER perpetual (licenses#244):
        it gets TOKEN_API_DEFAULT_EXPIRY_DAYS (365)."""
        from datetime import UTC, datetime, timedelta

        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 201, response.text
        expires_at = datetime.fromisoformat(response.json()["expires_at"])
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((expires_at - expected).total_seconds()) < 120

    def test_create_token_nonexistent_license(self, auth_client_admin: TestClient):
        """Create token for nonexistent license returns 404."""
        response = auth_client_admin.post(f"/licenses/{uuid4()}/tokens")
        assert response.status_code == 404

    def test_create_token_invalid_license_id(self, auth_client_admin: TestClient):
        """Create token with invalid license_id format returns 400."""
        response = auth_client_admin.post("/licenses/not-a-uuid/tokens")
        assert response.status_code == 400

    def test_create_token_other_tenant_license(
        self, auth_client_admin: TestClient, seed_other_tenant_license
    ):
        """Create token for another tenant's license returns 404."""
        license_id = str(seed_other_tenant_license.id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 404
        assert response.json()["detail"] == "License not found"

    def test_create_token_db_error(self, auth_client_admin: TestClient, full_seed_data):
        """Covers lines 899-901: exception handler in create_license_token."""
        from models import DatabaseManager

        license_id = str(full_seed_data["license"].id)
        with patch.object(
            DatabaseManager, "create_token", side_effect=RuntimeError("Token creation failed")
        ):
            response = auth_client_admin.post(f"/licenses/{license_id}/tokens")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to create token"


class TestRegenerateTokenEndpoint:
    """Test /licenses/{license_id}/regenerate-token endpoint."""

    def test_regenerate_token(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can regenerate a token for a license."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/regenerate-token")
        assert response.status_code == 200
        data = response.json()
        assert "token" in data

    def test_regenerate_token_nonexistent_license(self, auth_client_admin: TestClient):
        """Regenerate token for nonexistent license returns 404."""
        response = auth_client_admin.post(f"/licenses/{uuid4()}/regenerate-token")
        assert response.status_code == 404

    def test_regenerate_token_other_tenant(
        self, auth_client_admin: TestClient, seed_other_tenant_license
    ):
        """Regenerate token for another tenant's license returns 403."""
        license_id = str(seed_other_tenant_license.id)
        response = auth_client_admin.post(f"/licenses/{license_id}/regenerate-token")
        assert response.status_code == 403
        assert response.json()["detail"] == "Not your license"

    def test_regenerate_token_db_error(self, auth_client_admin: TestClient, full_seed_data):
        """Covers lines 722-724: exception handler in regenerate_token."""
        from models import DatabaseManager

        license_id = str(full_seed_data["license"].id)
        # regenerate_token now performs revoke+mint atomically via
        # regenerate_token_for_license; patch that to exercise the 500 handler.
        with patch.object(
            DatabaseManager,
            "regenerate_token_for_license",
            side_effect=RuntimeError("DB error during atomic regenerate"),
        ):
            response = auth_client_admin.post(f"/licenses/{license_id}/regenerate-token")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to regenerate token"


# (case_id, status, starts_at delta in days, expires_at delta in days or None,
#  expected EFFECTIVE status, expected STORED status). Deltas are resolved at
# test-run time, not collection time.
_REGEN_GATE_INVALID_STATES = [
    ("time_expired_but_status_still_active", "active", -400, -21, "expired", "active"),
    ("status_expired", "expired", -400, -21, "expired", "expired"),
    ("revoked", "revoked", -10, None, "revoked", "revoked"),
    ("not_yet_started", "active", 7, 90, "pending", "active"),
]


class TestRegenerateTokenValidityGate:
    """regenerate-token must CHECK BEFORE DESTROY.

    ``regenerate_token_for_license`` atomically bulk-revokes EVERY live token for
    the license and mints a replacement. On main the route ran it after only a
    404 + tenant-ownership check — with NO validity check at all. On an expired /
    revoked / not-yet-started license that DESTROYED the caller's still-working
    tokens and handed back a replacement that ``/license/introspect`` immediately
    rejects as ``license_inactive``. Raw tokens are shown once and only the
    SHA-256 hash is stored, so the loss is UNRECOVERABLE.

    fr's console reaches this via
    ``fr/backend/src/routes/me/licenses.ts`` -> ``licensesClient.regenerateToken(id)``.
    """

    TENANT = "01000000-0000-7000-8000-000000000001"

    def _licensed(
        self,
        db_manager,
        seed_app,
        seed_plan,
        sub: str,
        *,
        status: str,
        starts_days: int,
        expires_days: int | None,
    ):
        """Create (subject, license, one live token) under the admin client's tenant."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        subject = db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="secure-apps")
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id=self.TENANT,
            status=status,
            starts_at=now + timedelta(days=starts_days),
            expires_at=None if expires_days is None else now + timedelta(days=expires_days),
        )
        _token, raw = db_manager.create_token(
            license_id=UUID(str(lic.id)), token_type="api", expires_at=None
        )
        return lic, raw

    @pytest.mark.parametrize(
        "case_id,lic_status,starts_days,expires_days,expected_effective,expected_stored",
        [pytest.param(*case, id=case[0]) for case in _REGEN_GATE_INVALID_STATES],
    )
    def test_regenerate_on_invalid_license_409_and_existing_token_survives(
        self,
        auth_client_admin: TestClient,
        db_manager,
        seed_app,
        seed_plan,
        case_id,
        lic_status,
        starts_days,
        expires_days,
        expected_effective,
        expected_stored,
    ):
        """409 carrying the EFFECTIVE status, AND the caller's pre-existing token
        is untouched. Both halves are RED on main (200 + token destroyed)."""
        from models import Token

        lic, raw = self._licensed(
            db_manager,
            seed_app,
            seed_plan,
            f"regen-gate-{case_id}",
            status=lic_status,
            starts_days=starts_days,
            expires_days=expires_days,
        )

        response = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token")

        assert response.status_code == 409
        body = response.json()
        assert "token" not in body, "a refused regenerate must not mint anything"
        detail = body["detail"]
        assert detail["error"] == "license_not_active"
        assert detail["license_id"] == str(lic.id)
        assert detail["status"] == expected_effective
        assert detail["stored_status"] == expected_stored

        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == lic.id).all()
            assert len(tokens) == 1, "no replacement token may have been minted"
            assert tokens[0].revoked is False, "the caller's working token was destroyed"
            assert tokens[0].token_hash == Token.hash_token(raw)
        finally:
            session.close()

    def test_refused_regenerate_is_a_no_op_on_a_token_that_introspects_active(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """The observable half, end to end.

        A license that has aged out WITHOUT its status being transitioned (the
        production shape) still refuses the rotation; once renewed, the caller's
        ORIGINAL token introspects ``active: true`` exactly as before. On main the
        token is revoked by then and introspects ``active: false`` — permanently,
        because the raw replacement was returned once inside a response the caller
        treated as a failure.
        """
        from datetime import UTC, datetime, timedelta

        from models import License

        lic, raw = self._licensed(
            db_manager,
            seed_app,
            seed_plan,
            "regen-gate-live",
            status="active",
            starts_days=-1,
            expires_days=30,
        )
        before = auth_client_admin.post("/license/introspect", json={"token": raw})
        assert before.status_code == 200
        assert before.json()["active"] is True

        def _set_expiry(delta_days: int) -> None:
            session = db_manager.get_session()
            try:
                session.query(License).filter(License.id == lic.id).update(
                    {"expires_at": datetime.now(UTC) + timedelta(days=delta_days)}
                )
                session.commit()
            finally:
                session.close()

        _set_expiry(-1)  # aged out, status still 'active'
        assert auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token").status_code == 409

        _set_expiry(30)  # renewed
        after = auth_client_admin.post("/license/introspect", json={"token": raw})
        assert after.status_code == 200
        assert after.json()["active"] is True, (
            "the refused regenerate destroyed the caller's working token"
        )

    @pytest.mark.parametrize(
        "case_id,lic_status,starts_days,expires_days",
        [pytest.param(*case[:4], id=case[0]) for case in _REGEN_GATE_INVALID_STATES],
    )
    def test_regenerate_never_calls_the_destructive_rotation_for_invalid_license(
        self,
        auth_client_admin: TestClient,
        db_manager,
        seed_app,
        seed_plan,
        case_id,
        lic_status,
        starts_days,
        expires_days,
    ):
        """ORDERING assertion: the gate runs BEFORE the destructive call.

        Patching the rotation makes it a no-op, so token state can no longer
        distinguish "checked first" from "checked after the damage" — only
        call/no-call can. autospec so a signature-mismatched call still fails.
        """
        from models import DatabaseManager

        lic, _raw = self._licensed(
            db_manager,
            seed_app,
            seed_plan,
            f"regen-order-{case_id}",
            status=lic_status,
            starts_days=starts_days,
            expires_days=expires_days,
        )

        with patch.object(DatabaseManager, "regenerate_token_for_license", autospec=True) as rotate:
            response = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token")

        assert response.status_code == 409
        rotate.assert_not_called()

    def test_regenerate_on_active_license_still_rotates(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Positive control: the gate is not a blanket refusal. A genuinely active
        license still rotates — new token introspects active, old one does not."""
        lic, old_raw = self._licensed(
            db_manager,
            seed_app,
            seed_plan,
            "regen-gate-happy",
            status="active",
            starts_days=-1,
            expires_days=30,
        )

        response = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token")
        assert response.status_code == 200
        new_raw = response.json()["token"]
        assert new_raw != old_raw

        assert (
            auth_client_admin.post("/license/introspect", json={"token": new_raw}).json()["active"]
            is True
        )
        assert (
            auth_client_admin.post("/license/introspect", json={"token": old_raw}).json()["active"]
            is False
        )


class TestRegenerateTokenExpiryInheritance:
    """A rotation must never lengthen a credential's life.

    Orthogonal to the validity gate above: a perfectly VALID license could still
    launder a 1-day token into a never-expiring one, because the route passed no
    ``expires_at`` and the DB layer defaulted it to ``None`` (= perpetual, per
    ``Token.is_valid``). On the mint route perpetuity is at least an EXPLICIT
    choice (omit ``expires_days``); rotation made it the silent default.
    """

    def _live_token(self, db_manager, license_id):
        from models import Token

        session = db_manager.get_session()
        try:
            live = (
                session.query(Token)
                .filter(Token.license_id == license_id, Token.revoked.is_(False))
                .all()
            )
            assert len(live) == 1
            return live[0].expires_at
        finally:
            session.close()

    def test_rotating_a_short_lived_token_does_not_mint_a_perpetual_one(
        self, auth_client_admin: TestClient, db_manager, seed_license
    ):
        """RED on main: the replacement came back with ``expires_at = NULL``."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        original_expiry = datetime.now(UTC) + timedelta(days=1)
        db_manager.create_token(lic_id, token_type="api", expires_at=original_expiry)

        response = auth_client_admin.post(f"/licenses/{lic_id}/regenerate-token")
        assert response.status_code == 200

        new_expiry = self._live_token(db_manager, lic_id)
        assert new_expiry is not None, "the rotated token is PERPETUAL"
        assert new_expiry <= original_expiry, "the rotation lengthened the credential"

    def test_rotating_a_perpetual_token_keeps_it_perpetual(
        self, auth_client_admin: TestClient, db_manager, seed_license
    ):
        """Positive control: an expiry must not be imposed on a token that never
        had one."""
        lic_id = UUID(str(seed_license.id))
        insert_legacy_perpetual_token(db_manager, lic_id)

        response = auth_client_admin.post(f"/licenses/{lic_id}/regenerate-token")
        assert response.status_code == 200
        assert self._live_token(db_manager, lic_id) is None

    def test_the_response_reports_the_expiry_the_caller_did_not_choose(
        self, auth_client_admin: TestClient, db_manager, seed_license
    ):
        """The rotated token's lifetime is INHERITED, so the caller never picks it
        — and used to have no way to see it either. The raw token is shown once;
        its expiry has to come back in the same breath or it is unknowable without
        a second admin call."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        original_expiry = datetime.now(UTC) + timedelta(days=3)
        db_manager.create_token(lic_id, token_type="api", expires_at=original_expiry)

        body = auth_client_admin.post(f"/licenses/{lic_id}/regenerate-token").json()

        assert "expires_at" in body, "the caller cannot see what lifetime it was given"
        reported = datetime.fromisoformat(body["expires_at"])
        assert abs((reported - original_expiry).total_seconds()) < 1
        # ...and it is the SAME value the row carries, not a hopeful echo.
        assert abs((reported - self._live_token(db_manager, lic_id)).total_seconds()) < 1

    def test_a_perpetual_rotation_reports_null_rather_than_omitting_the_field(
        self, auth_client_admin: TestClient, db_manager, seed_license
    ):
        """Non-vacuity control for the test above: `null` must be a reported value,
        not an absent key, or a client cannot distinguish "perpetual" from "this
        server does not tell you"."""
        lic_id = UUID(str(seed_license.id))
        insert_legacy_perpetual_token(db_manager, lic_id)

        body = auth_client_admin.post(f"/licenses/{lic_id}/regenerate-token").json()

        assert "expires_at" in body
        assert body["expires_at"] is None


class TestRegenerateRefusesWhenEveryTokenHasExpired:
    """The license gate passes and the rotation is STILL destructive.

    ``TestRegenerateTokenChecksBeforeDestroy`` above closed the case where the
    LICENSE is expired/revoked/not-yet-started. This is the case that gate cannot
    see: an effectively-ACTIVE license — including a perpetual one — whose every
    unrevoked token has already expired. ``is_active()`` reads the license's own
    status/starts_at/expires_at and never looks at a token, so the 409 does not
    fire and rotation proceeds: it bulk-revokes the (expired) tokens and mints a
    replacement carrying their PAST expiry, which ``/license/introspect``
    immediately rejects as ``token_invalid``.

    Measured against production before this was written: 2 licenses were already
    in exactly that state, one ``active`` with ``expires_at IS NULL``.

    The remedy is a refusal, not a corrected timestamp — see
    ``models.AllTokensExpiredError``. Nothing is revoked, so the caller is never
    left worse off, and an expiry can be chosen explicitly on the mint route.
    """

    TENANT = "01000000-0000-7000-8000-000000000001"

    def _licensed_with_expired_token(self, db_manager, seed_app, seed_plan, sub: str):
        """An ACTIVE, perpetual license whose only unrevoked token died yesterday."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        subject = db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="secure-apps")
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id=self.TENANT,
            status="active",
            starts_at=now - timedelta(days=30),
            expires_at=None,
        )
        _token, raw = db_manager.create_token(
            license_id=UUID(str(lic.id)), token_type="api", expires_at=now - timedelta(days=1)
        )
        return lic, raw

    def test_the_license_gate_alone_would_let_this_through(self, db_manager, seed_app, seed_plan):
        """Non-vacuity control, and the reason this class exists.

        If the license were NOT effectively active the existing 409 would fire and
        the refusal below would prove nothing about tokens. Assert the precondition
        explicitly: this license is active, so any refusal must be token-derived.
        """
        lic, _raw = self._licensed_with_expired_token(db_manager, seed_app, seed_plan, "regen-live")

        assert db_manager.get_license(UUID(str(lic.id))).is_active() is True

    def test_refuses_with_409_all_tokens_expired(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """RED on main: 200 plus a token that is already dead."""
        lic, _raw = self._licensed_with_expired_token(db_manager, seed_app, seed_plan, "regen-409")

        response = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token")

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "all_tokens_expired"
        assert detail["license_id"] == str(lic.id)
        # The two 409s on this route must stay distinguishable: they have DIFFERENT
        # remedies (renew the license vs. mint a token), and a caller that cannot
        # tell them apart cannot act on either.
        #
        # ⚠️ NOT because "fr branches on this field" — it does not, and an earlier
        # version of this comment said so wrongly. `fr/backend/src/services/licenses.ts`
        # `request<T>` collapses any non-2xx into `throw new Error("Licenses API
        # error (409): <body text>")`, and `routes/me/licenses.ts` calls
        # `regenerateToken` with no try/catch — so today BOTH 409s surface in fr as
        # an unhandled 500. Grepped: `license_not_active` and `all_tokens_expired`
        # appear nowhere in fr's frontend or backend. That is fr's lane and is
        # recorded in the handoff; the structured detail is what makes fixing it
        # possible, not evidence that it has been fixed.
        assert detail["error"] != "license_not_active"

    def test_the_refusal_leaves_the_existing_token_untouched(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Nothing destroyed. The token is still expired — that is not the point —
        but it is still THERE and still unrevoked, so an operator can inspect it
        and no state was lost by asking."""
        from models import Token

        lic, _raw = self._licensed_with_expired_token(db_manager, seed_app, seed_plan, "regen-keep")

        assert auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token").status_code == 409

        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == UUID(str(lic.id))).all()
            assert len(tokens) == 1, "a replacement was minted despite the 409"
            assert tokens[0].revoked is False, "the refusal revoked the caller's token anyway"
        finally:
            session.close()

    def test_a_license_with_one_live_token_still_rotates(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """The control that keeps the refusal narrow: adding a single live token to
        the SAME shape of license restores a 200. Without this, a mutant that 409s
        every rotation would pass every test above.

        Deliberately asserts only the status and that a token came back — nothing
        about ``expires_at``. This control has to hold on BOTH sides of the change,
        so it must not depend on a field this branch introduces; otherwise it goes
        red on main for a reason that has nothing to do with the refusal it exists
        to bound.
        """
        from datetime import UTC, datetime, timedelta

        lic, raw_before = self._licensed_with_expired_token(
            db_manager, seed_app, seed_plan, "regen-ok"
        )
        db_manager.create_token(
            license_id=UUID(str(lic.id)),
            token_type="api",
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )

        response = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token")

        assert response.status_code == 200, response.text
        assert response.json()["token"] not in ("", raw_before)

    def test_the_replacement_is_usable_when_the_rotation_is_allowed(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """End-to-end: rotating a license that HAS a live token yields a token that
        introspects active. This is what the defect violated, expressed without
        reference to timestamps.

        ⚠️ Scoped deliberately to "has a live token with real time left". It is NOT
        the general claim that any 200 from this route implies a usable token —
        that claim would be false, and correctly so: rotation inherits the expiry
        it finds, so a license whose newest live token expires in 200 ms returns a
        replacement that dies in 200 ms. Rotation must never LENGTHEN a credential
        (``test_a_token_expiring_imminently_still_rotates`` pins the other side of
        that boundary), so the alternative would be worse than the symptom.
        """
        from datetime import UTC, datetime, timedelta

        lic, _raw = self._licensed_with_expired_token(db_manager, seed_app, seed_plan, "regen-use")
        db_manager.create_token(
            license_id=UUID(str(lic.id)),
            token_type="api",
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )

        new_raw = auth_client_admin.post(f"/licenses/{lic.id}/regenerate-token").json()["token"]

        introspected = auth_client_admin.post("/license/introspect", json={"token": new_raw}).json()
        assert introspected["active"] is True, introspected


class TestCreateUserLicenseRaceFallback:
    """``create_user_license``'s ``except IntegrityError:`` branch.

    This is the leg that makes the lazy expire-on-write safe under
    ``replicas: 2``: both replicas retire the corpse, both INSERT, and the loser
    hits ``uq_license_active_tenant_app_subject``. It was entirely uncovered — a
    mutant deleting the trailing ``raise`` survived the whole suite.
    """

    def _integrity_error(self):
        from sqlalchemy.exc import IntegrityError

        return IntegrityError("INSERT INTO license ...", {}, Exception("duplicate key"))

    def test_losing_the_insert_race_returns_the_winners_license(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """Loser of the unique-index race returns the winner's row, not a 500."""
        from models import DatabaseManager

        winner_subject = db_manager.create_or_get_subject(
            keycloak_sub="race-winner-sub", keycloak_realm="secure-apps"
        )
        winner = db_manager.create_license(
            subject_id=UUID(str(winner_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="active",
        )

        with (
            patch.object(
                DatabaseManager,
                "create_license",
                autospec=True,
                side_effect=self._integrity_error(),
            ),
            patch.object(
                DatabaseManager, "find_active_license", autospec=True, side_effect=[None, winner]
            ),
        ):
            response = auth_client_admin.post(
                "/licenses/user/race-loser-sub",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
            )

        assert response.status_code == 201
        assert response.json()["id"] == str(winner.id)

    def test_integrity_error_with_no_winner_is_not_swallowed(
        self,
        auth_client_admin: TestClient,
        db_manager,
        seed_app,
        seed_plan,
        caplog: pytest.LogCaptureFixture,
    ):
        """If the re-select finds nothing the IntegrityError must PROPAGATE (500),
        never be swallowed into a success. Pins the trailing ``raise``.

        The status code ALONE cannot pin it: deleting the ``raise`` also yields a
        500, because control falls through to ``logger.info(f"... {license.id}")``
        with ``license`` unbound and the route's broad handler turns the resulting
        UnboundLocalError into the same ``Failed to create license`` response. So
        assert on WHICH exception the handler logged — that is the only signal
        that distinguishes a propagated IntegrityError from a fall-through. It is
        also stable: the log line keeps ``{e}`` even once the HTTP ``detail`` stops
        echoing exception text.
        """
        import logging

        from models import DatabaseManager

        with (
            patch.object(
                DatabaseManager,
                "create_license",
                autospec=True,
                side_effect=self._integrity_error(),
            ),
            patch.object(
                DatabaseManager, "find_active_license", autospec=True, side_effect=[None, None]
            ),
            caplog.at_level(logging.ERROR),
        ):
            response = auth_client_admin.post(
                "/licenses/user/race-orphan-sub",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
            )

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to create license"
        assert "duplicate key" in caplog.text, (
            "the IntegrityError did not reach the handler — the re-select's "
            "trailing `raise` is missing and control fell through"
        )
        assert "UnboundLocalError" not in caplog.text


class TestAppEndpoints:
    """Test app CRUD endpoints."""

    def test_create_app(self, auth_client_admin: TestClient):
        """Admin can create an app."""
        response = auth_client_admin.post(
            "/apps",
            json={"key": "new-app-key", "name": "New App", "description": "A new app"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["key"] == "new-app-key"
        assert data["name"] == "New App"

    def test_create_app_idempotent(self, auth_client_admin: TestClient, seed_app):
        """Creating app with existing key returns the existing app."""
        response = auth_client_admin.post(
            "/apps",
            json={"key": str(seed_app.key), "name": "Different Name"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == seed_app.key

    def test_list_apps(self, auth_client_admin: TestClient, seed_app):
        """Admin can list apps."""
        response = auth_client_admin.get("/apps")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        app_keys = [a["key"] for a in data]
        assert seed_app.key in app_keys

    def test_get_app_by_id(self, auth_client_admin: TestClient, seed_app):
        """Admin can get an app by UUID."""
        response = auth_client_admin.get(f"/apps/{seed_app.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == seed_app.key

    def test_get_app_by_key(self, auth_client_admin: TestClient, seed_app):
        """Admin can get an app by key."""
        response = auth_client_admin.get(f"/apps/{seed_app.key}")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == seed_app.key

    def test_get_app_not_found(self, auth_client_admin: TestClient):
        """Get nonexistent app returns 404."""
        response = auth_client_admin.get(f"/apps/{uuid4()}")
        assert response.status_code == 404

    def test_list_app_plans(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """Admin can list plans for an app."""
        response = auth_client_admin.get(f"/apps/{seed_app.id}/plans")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        found = next(p for p in data if p["id"] == str(seed_plan.id))
        assert found["code"] == seed_plan.code
        assert found["app_id"] == str(seed_app.id)

    def test_list_app_plans_not_found(self, auth_client_admin: TestClient):
        """List plans for nonexistent app returns 404."""
        response = auth_client_admin.get(f"/apps/{uuid4()}/plans")
        assert response.status_code == 404

    def test_list_app_plans_by_key(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """List plans using app key."""
        response = auth_client_admin.get(f"/apps/{seed_app.key}/plans")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for p in data:
            assert p["app_id"] == str(seed_app.id)

    def test_list_app_licenses(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can list licenses for an app."""
        app = full_seed_data["app"]
        response = auth_client_admin.get(f"/apps/{app.id}/licenses")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for lic in data:
            assert lic["app_id"] == str(app.id)

    def test_list_app_licenses_not_found(self, auth_client_admin: TestClient):
        """List licenses for nonexistent app returns 404."""
        response = auth_client_admin.get(f"/apps/{uuid4()}/licenses")
        assert response.status_code == 404

    def test_list_app_licenses_by_key(self, auth_client_admin: TestClient, full_seed_data):
        """List licenses using app key."""
        app = full_seed_data["app"]
        response = auth_client_admin.get(f"/apps/{app.key}/licenses")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for lic in data:
            assert lic["app_id"] == str(app.id)

    def test_create_app_db_error(self, auth_client_admin: TestClient):
        """Covers lines 933-935: exception handler in create_app."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "get_app_by_key", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.post("/apps", json={"key": "err-app", "name": "Err"})
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to create app"

    def test_list_apps_db_error(self, auth_client_admin: TestClient):
        """Covers lines 954-956: exception handler in list_apps."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "list_apps", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get("/apps")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list apps"

    def test_get_app_db_error(self, auth_client_admin: TestClient, seed_app):
        """Covers lines 987-989: exception handler in get_app."""
        from models import DatabaseManager

        with (
            patch.object(DatabaseManager, "get_app", side_effect=RuntimeError("DB error")),
            patch.object(DatabaseManager, "get_app_by_key", side_effect=RuntimeError("DB error")),
        ):
            response = auth_client_admin.get(f"/apps/{seed_app.id}")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to get app"

    def test_list_app_plans_db_error(self, auth_client_admin: TestClient, seed_app):
        """Covers lines 1021-1023: exception handler in list_app_plans."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "list_plans", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get(f"/apps/{seed_app.id}/plans")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list app plans"

    def test_list_app_licenses_db_error(self, auth_client_admin: TestClient, seed_app):
        """Covers lines 1059-1061: exception handler in list_app_licenses."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "list_licenses", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get(f"/apps/{seed_app.id}/licenses")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list app licenses"


class TestPlanEndpoints:
    """Test plan CRUD endpoints."""

    def test_create_plan(self, auth_client_admin: TestClient, seed_app):
        """Admin can create a plan."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "new-plan",
                "name": "New Plan",
                "description": "A new plan",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["code"] == "new-plan"
        assert data["app_id"] == str(seed_app.id)
        assert data["name"] == "New Plan"

    def test_create_plan_with_region(self, auth_client_admin: TestClient, seed_app, seed_region):
        """Admin can create a plan with a region."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "regional-plan",
                "name": "Regional Plan",
                "region_id": str(seed_region.id),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["region_id"] == str(seed_region.id)

    def test_create_plan_quota_zero_block_all(self, auth_client_admin: TestClient, seed_app):
        """quota_monthly=0 (block-all) is a valid plan and is stored as 0."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "blockall-plan",
                "name": "Block All Plan",
                "quota_monthly": 0,
            },
        )
        assert response.status_code == 201
        assert response.json()["quota_monthly"] == 0

    def test_create_plan_negative_quota_rejected(self, auth_client_admin: TestClient, seed_app):
        """Negative quota_monthly is invalid and rejected with 422 (Field ge=0)."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "neg-quota-plan",
                "name": "Negative Quota Plan",
                "quota_monthly": -1,
            },
        )
        assert response.status_code == 422

    def test_update_plan_negative_quota_rejected(self, auth_client_admin: TestClient, seed_plan):
        """PATCH with negative quota_monthly is rejected with 422 (Field ge=0)."""
        response = auth_client_admin.patch(
            f"/plans/{seed_plan.id}",
            json={"quota_monthly": -1},
        )
        assert response.status_code == 422

    def test_create_plan_negative_token_budget_rejected(
        self, auth_client_admin: TestClient, seed_app
    ):
        """Negative ``token_budget_monthly`` is rejected with 422 (Field ge=0).

        Not cosmetic symmetry with ``quota_monthly``: ``_provision_budget``
        pushes a budget only when the plan value ``is not None and >= 0``. A
        stored -5 therefore falls through that guard exactly like NULL does, no
        budget row is created in billing, and the licence is enforced as
        UNLIMITED — the same silently-unlimited failure the block-all (0) fix
        exists to prevent, re-entered through the create endpoint.
        """
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "neg-token-budget-plan",
                "name": "Negative Token Budget Plan",
                "token_budget_monthly": -5,
            },
        )
        assert response.status_code == 422

    def test_update_plan_negative_token_budget_rejected(
        self, auth_client_admin: TestClient, seed_plan
    ):
        """PATCH with negative ``token_budget_monthly`` is rejected with 422.

        The update path needs its own floor: validating only on create leaves a
        one-request route to the same silently-unlimited state.
        """
        response = auth_client_admin.patch(
            f"/plans/{seed_plan.id}",
            json={"token_budget_monthly": -5},
        )
        assert response.status_code == 422

    def test_create_plan_token_budget_zero_block_all(self, auth_client_admin: TestClient, seed_app):
        """Non-vacuity of the floor above: 0 (block all) is still ACCEPTED.

        A ``gt=0`` typo would make both 422 tests pass while breaking the
        block-all budget this branch fixed, so the boundary is pinned here.
        """
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "zero-token-budget-plan",
                "name": "Zero Token Budget Plan",
                "token_budget_monthly": 0,
            },
        )
        assert response.status_code == 201
        assert response.json()["token_budget_monthly"] == 0

    def test_update_plan_token_budget_zero_block_all(
        self, auth_client_admin: TestClient, seed_plan
    ):
        """PATCH ``token_budget_monthly=0`` is accepted and stored as 0."""
        response = auth_client_admin.patch(
            f"/plans/{seed_plan.id}",
            json={"token_budget_monthly": 0},
        )
        assert response.status_code == 200
        assert response.json()["token_budget_monthly"] == 0

    def test_update_plan_quota_zero_block_all(self, auth_client_admin: TestClient, seed_plan):
        """PATCH quota_monthly=0 (block-all) is accepted and stored as 0."""
        response = auth_client_admin.patch(
            f"/plans/{seed_plan.id}",
            json={"quota_monthly": 0},
        )
        assert response.status_code == 200
        assert response.json()["quota_monthly"] == 0

    def test_db_check_rejects_negative_quota(self, db_manager, seed_app):
        """Defence in depth: the DB CheckConstraint ck_plan_quota_monthly_nonneg
        rejects a negative quota even if a non-API path tries to write one.
        Proves the constraint is non-vacuous (0 and NULL are still accepted)."""
        import pytest as _pytest
        from sqlalchemy.exc import IntegrityError

        with _pytest.raises(IntegrityError):
            db_manager.create_plan(
                app_id=seed_app.id,
                code="db-neg-quota",
                name="DB Negative Quota",
                quota_monthly=-5,
            )
        # 0 (block-all) is accepted by the same constraint.
        p = db_manager.create_plan(
            app_id=seed_app.id,
            code="db-zero-quota",
            name="DB Zero Quota",
            quota_monthly=0,
        )
        assert p.quota_monthly == 0

    def test_create_plan_invalid_app_id(self, auth_client_admin: TestClient):
        """Create plan with invalid app_id returns 422 (Pydantic UUID validation)."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": "not-a-uuid",
                "code": "bad-plan",
                "name": "Bad Plan",
            },
        )
        assert response.status_code == 422

    def test_create_plan_invalid_region_id(self, auth_client_admin: TestClient, seed_app):
        """Create plan with invalid region_id returns 422 (Pydantic UUID validation)."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "bad-region-plan",
                "name": "Bad Region Plan",
                "region_id": "not-a-uuid",
            },
        )
        assert response.status_code == 422

    def test_create_plan_app_not_found(self, auth_client_admin: TestClient):
        """Create plan with nonexistent app_id returns 404."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(uuid4()),
                "code": "orphan-plan",
                "name": "Orphan Plan",
            },
        )
        assert response.status_code == 404

    def test_create_plan_region_not_found(self, auth_client_admin: TestClient, seed_app):
        """Create plan with nonexistent region_id returns 404."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "orphan-region-plan",
                "name": "Orphan Region Plan",
                "region_id": str(uuid4()),
            },
        )
        assert response.status_code == 404

    def test_list_plans(self, auth_client_admin: TestClient, seed_plan):
        """Admin can list plans."""
        response = auth_client_admin.get("/plans")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        found = next(p for p in data if p["id"] == str(seed_plan.id))
        assert found["code"] == seed_plan.code
        assert found["name"] == seed_plan.name

    def test_list_plans_with_app_filter(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """List plans filtered by app_id."""
        response = auth_client_admin.get(f"/plans?app_id={seed_app.id}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for p in data:
            assert p["app_id"] == str(seed_app.id)

    def test_list_plans_invalid_uuid(self, auth_client_admin: TestClient):
        """List plans with invalid UUID filter returns 400."""
        response = auth_client_admin.get("/plans?app_id=not-a-uuid")
        assert response.status_code == 400

    def test_get_plan(self, auth_client_admin: TestClient, seed_plan):
        """Admin can get a plan by ID."""
        response = auth_client_admin.get(f"/plans/{seed_plan.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == seed_plan.code

    def test_get_plan_not_found(self, auth_client_admin: TestClient):
        """Get nonexistent plan returns 404."""
        response = auth_client_admin.get(f"/plans/{uuid4()}")
        assert response.status_code == 404

    def test_get_plan_invalid_uuid(self, auth_client_admin: TestClient):
        """Get plan with invalid UUID returns 400."""
        response = auth_client_admin.get("/plans/not-a-uuid")
        assert response.status_code == 400

    def test_create_plan_duplicate_code(self, auth_client_admin: TestClient, seed_app):
        """Covers lines 1125-1129: IntegrityError when creating duplicate plan code."""
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        from models import DatabaseManager

        with patch.object(
            DatabaseManager,
            "create_plan",
            side_effect=SAIntegrityError("INSERT", {}, Exception("UNIQUE constraint failed")),
        ):
            response = auth_client_admin.post(
                "/plans",
                json={"app_id": str(seed_app.id), "code": "dup-plan", "name": "Dup Plan"},
            )
            assert response.status_code == 409
            assert "already exists" in response.json()["detail"]

    def test_create_plan_db_error(self, auth_client_admin: TestClient, seed_app):
        """Covers lines 1130-1132: exception handler in create_plan."""
        from models import DatabaseManager

        with patch.object(
            DatabaseManager, "create_plan", side_effect=RuntimeError("DB write error")
        ):
            response = auth_client_admin.post(
                "/plans",
                json={"app_id": str(seed_app.id), "code": "error-plan", "name": "Error Plan"},
            )
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to create plan"

    def test_list_plans_db_error(self, auth_client_admin: TestClient):
        """Covers lines 1160-1162: exception handler in list_plans."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "list_plans", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get("/plans")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list plans"

    def test_get_plan_db_error(self, auth_client_admin: TestClient, seed_plan):
        """Covers lines 1192-1194: exception handler in get_plan."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "get_plan", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get(f"/plans/{seed_plan.id}")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to get plan"


class TestRegionEndpoints:
    """Test region CRUD endpoints."""

    def test_create_region(self, auth_client_admin: TestClient):
        """Admin can create a region."""
        response = auth_client_admin.post(
            "/regions",
            json={"code": "new-region", "name": "New Region", "active": True},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["code"] == "new-region"
        assert data["name"] == "New Region"
        assert data["active"] is True

    def test_create_region_idempotent(self, auth_client_admin: TestClient, seed_region):
        """Creating region with existing code returns the existing region."""
        response = auth_client_admin.post(
            "/regions",
            json={"code": str(seed_region.code), "name": "Different Name"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == seed_region.code

    def test_list_regions(self, auth_client_admin: TestClient, seed_region):
        """Admin can list regions."""
        response = auth_client_admin.get("/regions")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        found = next(r for r in data if r["id"] == str(seed_region.id))
        assert found["code"] == seed_region.code
        assert found["name"] == seed_region.name

    def test_list_regions_include_inactive(self, auth_client_admin: TestClient, db_manager):
        """List regions including inactive."""
        db_manager.create_region(code="active-region-2", name="Active 2", active=True)
        db_manager.create_region(code="inactive-region", name="Inactive", active=False)
        response = auth_client_admin.get("/regions?active_only=false")
        assert response.status_code == 200
        data = response.json()
        codes = [r["code"] for r in data]
        assert "inactive-region" in codes
        # Also verify at least one active region is present
        active_regions = [r for r in data if r["active"] is True]
        assert len(active_regions) >= 1

    def test_create_region_db_error(self, auth_client_admin: TestClient):
        """Covers lines 1228-1230: exception handler in create_region."""
        from models import DatabaseManager

        with patch.object(
            DatabaseManager, "get_region_by_code", side_effect=RuntimeError("DB error")
        ):
            response = auth_client_admin.post(
                "/regions", json={"code": "err", "name": "Error Region"}
            )
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to create region"

    def test_list_regions_db_error(self, auth_client_admin: TestClient):
        """Covers lines 1250-1252: exception handler in list_regions."""
        from models import DatabaseManager

        with patch.object(DatabaseManager, "list_regions", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.get("/regions")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to list regions"


class TestDeleteEndpoints:
    """Test delete endpoints."""

    def test_delete_app(self, auth_client_admin: TestClient, db_manager):
        """Admin can delete an app."""
        app = db_manager.create_app(key="delete-me", name="Delete Me", issuers=("platform",))
        response = auth_client_admin.delete(f"/apps/{app.id}")
        assert response.status_code == 204

    def test_delete_app_not_found(self, auth_client_admin: TestClient):
        """Delete nonexistent app returns 404."""
        response = auth_client_admin.delete(f"/apps/{uuid4()}")
        assert response.status_code == 404

    def test_delete_app_invalid_uuid(self, auth_client_admin: TestClient):
        """Delete app with invalid UUID returns 400."""
        response = auth_client_admin.delete("/apps/not-a-uuid")
        assert response.status_code == 400

    def test_delete_app_cascades(self, auth_client_admin: TestClient, full_seed_data):
        """Deleting an app cascades to its plans, licenses, tokens.

        Now requires explicit blast-radius confirmation: the seeded app carries a
        licence, so the unconfirmed call refuses (see
        ``TestDeleteAppCrossTenantGuard``) and the count must be acknowledged.
        """
        app = full_seed_data["app"]
        radius = auth_client_admin.delete(f"/apps/{app.id}").json()["detail"]
        response = auth_client_admin.delete(f"/apps/{app.id}?confirm_licenses={radius['licenses']}")
        assert response.status_code == 204
        # Verify GET /apps/{id} returns 404 after deletion
        get_response = auth_client_admin.get(f"/apps/{app.id}")
        assert get_response.status_code == 404


class TestDeleteAppCrossTenantGuard:
    """``DELETE /apps/{app_id}`` must not silently destroy another tenant's licences.

    ``app`` is a GLOBAL catalogue row — no ``tenant_id``, globally unique ``key`` —
    and ``license.app_id`` is ``ON DELETE CASCADE``. So one call by any holder of
    the platform ``license-admin`` role hard-deleted EVERY tenant's licences for
    that app, cascaded their tokens, answered 204, logged nothing and left no
    audit row (this service has no audit table). There is no tenant predicate to
    add — the entity is global — so the guard is blast-radius confirmation, the
    same shape ``scripts/purge_test_apps.py`` has always applied to this cascade.

    Against ``origin/main`` every test in this class FAILS: main answers 204 and
    the other tenant's licence is gone.
    """

    def test_unconfirmed_delete_refuses_and_spares_the_other_tenant(
        self,
        auth_client_admin: TestClient,
        db_manager,
        seed_app,
        seed_other_tenant_license,
    ):
        """Tenant B's licence survives an unconfirmed delete driven by tenant A."""
        from models import App, License

        other_license_id = UUID(str(seed_other_tenant_license.id))
        assert str(seed_other_tenant_license.tenant_id) != "01000000-0000-7000-8000-000000000001"

        response = auth_client_admin.delete(f"/apps/{seed_app.id}")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "cascade_requires_confirmation"
        # The refusal is the only place an operator learns the radius.
        assert detail["licenses"] >= 1
        assert str(seed_other_tenant_license.tenant_id) in detail["tenants"]

        # The destruction did NOT happen: the other tenant's licence is intact.
        session = db_manager.get_session()
        try:
            assert session.query(License).filter(License.id == other_license_id).first() is not None
            assert session.query(App).filter(App.id == seed_app.id).first() is not None
        finally:
            session.close()

    def test_wrong_confirmation_count_refuses(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_other_tenant_license
    ):
        """A confirmation that does not match the live count is a mismatch, not a delete."""
        from models import App

        response = auth_client_admin.delete(f"/apps/{seed_app.id}?confirm_licenses=999")

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "cascade_count_mismatch"

        session = db_manager.get_session()
        try:
            assert session.query(App).filter(App.id == seed_app.id).first() is not None
        finally:
            session.close()

    def test_confirmed_delete_proceeds(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_other_tenant_license
    ):
        """The named admin path still works — the guard refuses, it does not block."""
        from models import App

        radius = auth_client_admin.delete(f"/apps/{seed_app.id}").json()["detail"]

        response = auth_client_admin.delete(
            f"/apps/{seed_app.id}?confirm_licenses={radius['licenses']}"
        )

        assert response.status_code == 204
        session = db_manager.get_session()
        try:
            assert session.query(App).filter(App.id == seed_app.id).first() is None
        finally:
            session.close()

    def test_app_without_licences_needs_no_confirmation(
        self, auth_client_admin: TestClient, db_manager
    ):
        """Zero blast radius means zero friction — the guard is proportionate."""
        app = db_manager.create_app(
            key="no-licences-here", name="No Licences", issuers=("platform",)
        )
        assert auth_client_admin.delete(f"/apps/{app.id}").status_code == 204

    def test_delete_plan(self, auth_client_admin: TestClient, db_manager, seed_app):
        """Admin can delete a plan with no licenses."""
        plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="deletable-plan",
            name="Deletable Plan",
        )
        response = auth_client_admin.delete(f"/plans/{plan.id}")
        assert response.status_code == 204

    def test_delete_plan_not_found(self, auth_client_admin: TestClient):
        """Delete nonexistent plan returns 404."""
        response = auth_client_admin.delete(f"/plans/{uuid4()}")
        assert response.status_code == 404

    def test_delete_plan_invalid_uuid(self, auth_client_admin: TestClient):
        """Delete plan with invalid UUID returns 400."""
        response = auth_client_admin.delete("/plans/not-a-uuid")
        assert response.status_code == 400

    def test_delete_plan_conflict(self, auth_client_admin: TestClient, full_seed_data):
        """Delete plan with active licenses returns 409."""
        plan = full_seed_data["plan"]
        response = auth_client_admin.delete(f"/plans/{plan.id}")
        assert response.status_code == 409

    def test_delete_user_licenses(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can delete all licenses for a user.

        ⚠️ UPDATED FOR THE NEW CONTRACT. ``LICENSE_SOFT_DELETE`` now defaults True,
        so this route requires ``confirm_licenses`` equal to the live count — this
        test's subject is the DELETE succeeding and the licences leaving the
        listing, NOT the confirmation gate, so it supplies the confirmation and
        goes on measuring what it always measured. The radius is read out of the
        refusal (which is fail-closed and destroys nothing) exactly as
        ``test_confirmed_delete_proceeds`` above does, rather than hard-coding a
        count the fixtures are free to change. The 409 itself is pinned by
        ``test_soft_delete_is_the_default.py::TestRetentionIsTheDefault::
        test_an_unconfirmed_delete_no_longer_destroys_anything``.

        ⚠️ "gone" now means "no longer LISTED": the row is retained and stamped and
        ``GET /licenses`` filters ``deleted_at``. The assertion below is unchanged
        and is exactly the consumer-visible property that must not regress.
        """
        subject = full_seed_data["subject"]
        radius = auth_client_admin.delete(f"/licenses/user/{subject.keycloak_sub}").json()["detail"]
        response = auth_client_admin.delete(
            f"/licenses/user/{subject.keycloak_sub}?confirm_licenses={radius['licenses']}"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["deleted"] >= 1
        # Verify licenses are gone by listing them
        list_response = auth_client_admin.get("/licenses")
        list_data = list_response.json()
        remaining_for_subject = [lic for lic in list_data if lic["subject_id"] == str(subject.id)]
        assert len(remaining_for_subject) == 0

    def test_delete_user_licenses_not_found(self, auth_client_admin: TestClient):
        """Delete licenses for nonexistent user returns 404."""
        response = auth_client_admin.delete("/licenses/user/nonexistent-sub")
        assert response.status_code == 404

    def test_delete_user_licenses_by_uuid(self, auth_client_admin: TestClient, full_seed_data):
        """Delete licenses by subject UUID.

        The subject is the UUID branch of the subject lookup (vs ``keycloak_sub``
        above), not the confirmation gate — so it supplies the confirmation the
        armed default now requires and keeps measuring the lookup.
        """
        subject = full_seed_data["subject"]
        radius = auth_client_admin.delete(f"/licenses/user/{subject.id}").json()["detail"]
        response = auth_client_admin.delete(
            f"/licenses/user/{subject.id}?confirm_licenses={radius['licenses']}"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["deleted"] >= 1

    def test_delete_app_db_error(self, auth_client_admin: TestClient, db_manager):
        """Covers lines 1284-1286: exception handler in delete_app."""
        from models import DatabaseManager

        app = db_manager.create_app(key="err-delete-app", name="Err", issuers=("platform",))
        with patch.object(DatabaseManager, "delete_app", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.delete(f"/apps/{app.id}")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to delete app"

    def test_delete_plan_db_error(self, auth_client_admin: TestClient, db_manager, seed_app):
        """Covers lines 1321-1323: exception handler in delete_plan."""
        from models import DatabaseManager

        plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)), code="err-del-plan", name="Err Plan"
        )
        with patch.object(DatabaseManager, "delete_plan", side_effect=RuntimeError("DB error")):
            response = auth_client_admin.delete(f"/plans/{plan.id}")
            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to delete plan"

    def test_delete_user_licenses_db_error(self, auth_client_admin: TestClient, full_seed_data):
        """The exception handler in ``delete_user_licenses``, on the DEFAULT path.

        ⚠️ UPDATED FOR THE NEW CONTRACT, AND THE PATCH TARGET MOVED WITH IT. This
        used to patch ``delete_licenses_for_subject``; with ``LICENSE_SOFT_DELETE``
        defaulting True the route calls ``soft_delete_licenses_for_subject``
        instead, so that patch is now INERT — the test would have measured the
        confirmation gate (409) rather than the 500 it names. Two changes are
        needed and both are mechanical: confirm the delete so control reaches the
        DB call at all, and patch the method the default path actually invokes.
        The hard path's handler keeps its own sibling below.
        """
        from models import DatabaseManager

        subject = full_seed_data["subject"]
        radius = auth_client_admin.delete(f"/licenses/user/{subject.keycloak_sub}").json()["detail"]
        with patch.object(
            DatabaseManager,
            "soft_delete_licenses_for_subject",
            side_effect=RuntimeError("DB error"),
        ):
            response = auth_client_admin.delete(
                f"/licenses/user/{subject.keycloak_sub}?confirm_licenses={radius['licenses']}"
            )
            assert response.status_code == 500, response.text
            assert response.json()["detail"] == "Failed to delete licenses"

    def test_delete_user_licenses_db_error_on_the_disarmed_hard_path(
        self, auth_client_admin: TestClient, full_seed_data, monkeypatch
    ):
        """The SIBLING that keeps the coverage the test above used to hold.

        ``LICENSE_SOFT_DELETE=false`` is a supported opt-out, and on it the route
        still calls ``delete_licenses_for_subject`` with no confirmation. Dropping
        this when the patch target moved would have quietly retired the only
        exercise of that branch's 500.
        """
        import main
        from models import DatabaseManager

        monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", False, raising=True)
        subject = full_seed_data["subject"]
        with patch.object(
            DatabaseManager,
            "delete_licenses_for_subject",
            side_effect=RuntimeError("DB error"),
        ):
            response = auth_client_admin.delete(f"/licenses/user/{subject.keycloak_sub}")
            assert response.status_code == 500, response.text
            assert response.json()["detail"] == "Failed to delete licenses"


class TestOpenAPISchema:
    """Test custom OpenAPI schema generation."""

    def test_openapi_has_bearer_auth(self, client: TestClient):
        """OpenAPI schema includes BearerAuth security scheme."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        security_schemes = schema["components"]["securitySchemes"]
        assert "BearerAuth" in security_schemes
        assert security_schemes["BearerAuth"]["type"] == "http"
        assert security_schemes["BearerAuth"]["scheme"] == "bearer"

    def test_openapi_has_tenant_header(self, client: TestClient):
        """OpenAPI schema includes X-Tenant-Id parameter."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        params = schema["components"]["parameters"]
        assert "TenantIdHeader" in params
        assert params["TenantIdHeader"]["name"] == "X-Tenant-Id"

    def test_openapi_marks_protected_paths_with_bearer_security(self, client: TestClient):
        """Protected operations advertise BearerAuth so Swagger sends Authorization."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()

        assert schema["paths"]["/licenses"]["get"]["security"] == [{"BearerAuth": []}]
        assert schema["paths"]["/license/revoke"]["post"]["security"] == [{"BearerAuth": []}]

    def test_only_the_tokenless_byo_mode_get_lacks_bearer_security(self, client: TestClient):
        """EXACT set, not a floor: exactly one operation under a protected prefix is
        published without ``security``, and it is the tokenless S2S byo-mode GET.

        ``custom_openapi`` derives ``security`` from ``PROTECTED_PREFIXES``, but
        ``AuthMiddleware.dispatch`` exempts this GET via ``_BYO_MODE_OPEN_ROUTES``
        BEFORE the prefix check ever runs — so a prefix-only derivation advertised auth
        the route does not require.

        An exact set is what makes this a mutation proof. It goes red on: reverting
        ``custom_openapi`` (set empties), dropping the ``method == "get"`` guard
        (``patch`` joins), broadening the regex (extra members), a future GET on the
        per-app shape being stamped without a decision, and the template-matching
        coincidence quietly ceasing to hold (set empties).
        """
        from auth.middleware import PROTECTED_PREFIXES

        schema = client.get("/openapi.json").json()
        unsecured = {
            (method, path)
            for path, item in schema["paths"].items()
            if any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)
            for method, operation in item.items()
            if isinstance(operation, dict) and "security" not in operation
        }
        assert unsecured == {("get", "/tenants/{name_or_id}/byo-mode")}

    def test_byo_mode_write_variants_keep_bearer_security(self, client: TestClient):
        """Only GET is exempt. A path-keyed fix would strip all four — this names them."""
        paths = client.get("/openapi.json").json()["paths"]
        tenant_shape = paths["/tenants/{name_or_id}/byo-mode"]
        app_shape = paths["/tenants/{name_or_id}/apps/{app_key}/byo-mode"]

        assert tenant_shape["patch"]["security"] == [{"BearerAuth": []}]
        assert app_shape["put"]["security"] == [{"BearerAuth": []}]
        assert app_shape["delete"]["security"] == [{"BearerAuth": []}]

    def test_the_document_declares_no_root_level_security(self, client: TestClient):
        """The mutant that survives BOTH tests above.

        In OpenAPI an operation with no ``security`` key inherits the ROOT-level
        ``security`` when one exists. Adding ``openapi_schema["security"] = [...]`` to
        ``custom_openapi`` leaves the byo-mode GET's key absent — so the exact-set test
        and the write-variant test both stay green — while every generated client
        starts sending, and expecting, a token on the tokenless route again.
        """
        assert "security" not in client.get("/openapi.json").json()

    def test_openapi_leaves_public_paths_without_bearer_security(self, client: TestClient):
        """Public operations remain unauthenticated in the schema."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()

        assert "security" not in schema["paths"]["/health"]["get"]
        assert "security" not in schema["paths"]["/license/introspect"]["post"]


class TestModelsCoverage:
    """Tests for models.py coverage gaps."""

    def test_license_is_active_true(self, seed_license):
        """Test is_active() returns True for active non-expired license."""
        assert seed_license.is_active() is True

    def test_token_is_valid_true(self, seed_token):
        """Test is_valid() returns True for valid token."""
        token, _ = seed_token
        assert token.is_valid() is True

    def test_database_manager_ssl_disable(self):
        """Test _append_ssl_params returns original URL when sslmode=disable."""
        import os
        from unittest.mock import patch

        from models import DatabaseManager

        mgr = DatabaseManager.__new__(DatabaseManager)
        with patch.dict(os.environ, {"POSTGRES_SSLMODE": "disable"}):
            result = mgr._append_ssl_params("postgresql://localhost/test")
        assert result == "postgresql://localhost/test"

    def test_database_manager_ssl_require(self):
        """Test _append_ssl_params appends sslmode=require."""
        import os
        from unittest.mock import patch

        from models import DatabaseManager

        mgr = DatabaseManager.__new__(DatabaseManager)
        with patch.dict(os.environ, {"POSTGRES_SSLMODE": "require"}):
            result = mgr._append_ssl_params("postgresql://localhost/test")
        assert "sslmode=require" in result

    def test_database_manager_ssl_verify_ca(self):
        """Test _append_ssl_params with verify-ca mode."""
        import os
        from unittest.mock import patch

        from models import DatabaseManager

        mgr = DatabaseManager.__new__(DatabaseManager)
        with patch.dict(os.environ, {"POSTGRES_SSLMODE": "verify-ca"}):
            result = mgr._append_ssl_params("postgresql://localhost/test")
        assert "sslmode=verify-ca" in result

    def test_database_manager_find_cert_not_found(self):
        """Test _find_cert returns None when cert not found."""
        from models import DatabaseManager

        mgr = DatabaseManager.__new__(DatabaseManager)
        result = mgr._find_cert("nonexistent-cert.pem")
        assert result is None

    def test_database_manager_ssl_with_existing_query_params(self):
        """Test _append_ssl_params uses & separator when URL already has query params."""
        import os
        from unittest.mock import patch

        from models import DatabaseManager

        mgr = DatabaseManager.__new__(DatabaseManager)
        with patch.dict(os.environ, {"POSTGRES_SSLMODE": "require"}):
            result = mgr._append_ssl_params("postgresql://localhost/test?pool_size=5")
        assert "&sslmode=require" in result

    def test_revoke_nonexistent_token(self, db_manager):
        """Revoking a token that doesn't exist returns False."""
        result = db_manager.revoke_token("does-not-exist-at-all")
        assert result is False

    def test_revoke_nonexistent_license(self, db_manager):
        """Revoking a nonexistent license returns False."""
        result = db_manager.revoke_license(uuid4())
        assert result is False

    def test_delete_nonexistent_app(self, db_manager):
        """Deleting a nonexistent app returns False."""
        result = db_manager.delete_app(uuid4())
        assert result is False

    def test_delete_nonexistent_plan(self, db_manager):
        """Deleting a nonexistent plan returns (False, None)."""
        found, reason = db_manager.delete_plan(uuid4())
        assert found is False
        assert reason is None

    def test_get_nonexistent_subject(self, db_manager):
        """Getting a nonexistent subject returns None."""
        result = db_manager.get_subject(uuid4())
        assert result is None

    def test_get_nonexistent_subject_by_keycloak_sub(self, db_manager):
        """Getting a nonexistent subject by keycloak_sub returns None."""
        result = db_manager.get_subject_by_keycloak_sub("nonexistent")
        assert result is None

    def test_get_nonexistent_app(self, db_manager):
        """Getting a nonexistent app returns None."""
        result = db_manager.get_app(uuid4())
        assert result is None

    def test_get_nonexistent_app_by_key(self, db_manager):
        """Getting a nonexistent app by key returns None."""
        result = db_manager.get_app_by_key("nonexistent-key")
        assert result is None

    def test_get_nonexistent_plan(self, db_manager):
        """Getting a nonexistent plan returns None."""
        result = db_manager.get_plan(uuid4())
        assert result is None

    def test_get_nonexistent_region(self, db_manager):
        """Getting a nonexistent region returns None."""
        result = db_manager.get_region(uuid4())
        assert result is None

    def test_get_nonexistent_license(self, db_manager):
        """Getting a nonexistent license returns None."""
        result = db_manager.get_license(uuid4())
        assert result is None

    def test_introspect_revoked_token(self, db_manager, seed_license):
        """Introspecting a revoked token returns active=False."""
        token, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
            expires_at=None,
        )
        db_manager.revoke_token(raw_token)
        result = db_manager.introspect_token(raw_token)
        assert result["active"] is False
        assert result["reason"] == "token_invalid"

    def test_delete_licenses_for_subject(self, db_manager, full_seed_data):
        """Delete all licenses for a subject within a tenant."""
        subject = full_seed_data["subject"]
        count = db_manager.delete_licenses_for_subject(
            subject_id=UUID(str(subject.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
        )
        assert count >= 1
        # Verify licenses are actually gone
        remaining = db_manager.list_licenses(
            subject_id=UUID(str(subject.id)), tenant_id="01000000-0000-7000-8000-000000000001"
        )
        assert len(remaining) == 0

    def test_revoke_tokens_for_license(self, db_manager, full_seed_data):
        """Revoke all tokens for a license."""
        license_obj = full_seed_data["license"]
        count = db_manager.revoke_tokens_for_license(UUID(str(license_obj.id)))
        assert count >= 1

    def test_delete_plan_with_licenses_conflict(self, db_manager, full_seed_data):
        """Deleting a plan with licenses returns (False, 'conflict')."""
        plan = full_seed_data["plan"]
        found, reason = db_manager.delete_plan(UUID(str(plan.id)))
        assert found is False
        assert reason == "conflict"

    def test_delete_app_cascades(self, db_manager, full_seed_data):
        """Deleting an app cascades to licenses and plans."""
        app = full_seed_data["app"]
        plan = full_seed_data["plan"]
        result = db_manager.delete_app(UUID(str(app.id)))
        assert result is True
        # Verify app is gone
        assert db_manager.get_app(UUID(str(app.id))) is None
        # Verify plan is also gone
        assert db_manager.get_plan(UUID(str(plan.id))) is None

    def test_database_manager_without_url_refuses(self):
        """DatabaseManager.__init__ with no URL raises — there is no localhost default."""
        from models import DatabaseManager

        for missing in (None, ""):
            with pytest.raises(ValueError, match="database URL"):
                DatabaseManager(database_url=missing)

    def test_database_manager_ssl_verify_ca_with_certs(self, tmp_path):
        """Test _append_ssl_params verify-ca with actual cert files."""
        import os
        from unittest.mock import patch

        from models import DatabaseManager

        # Create fake cert files
        ca_cert = tmp_path / "ca-cert.pem"
        ca_cert.write_text("fake-ca")
        client_cert = tmp_path / "licenses-client-cert.pem"
        client_cert.write_text("fake-cert")
        client_key = tmp_path / "licenses-client-key.pem"
        client_key.write_text("fake-key")

        mgr = DatabaseManager.__new__(DatabaseManager)
        env = {
            "POSTGRES_SSLMODE": "verify-ca",
            "PGSSLROOTCERT": str(ca_cert),
            "PGSSLCERT": str(client_cert),
            "PGSSLKEY": str(client_key),
        }
        with patch.dict(os.environ, env):
            result = mgr._append_ssl_params("postgresql://localhost/test")
        assert "sslmode=verify-ca" in result
        assert f"sslrootcert={ca_cert}" in result
        assert f"sslcert={client_cert}" in result
        assert f"sslkey={client_key}" in result

    def test_list_plans_with_region_filter(self, db_manager, seed_app, seed_region):
        """Test list_plans with region_id filter."""
        regional_plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="region-filtered",
            name="Regional Plan",
            region_id=UUID(str(seed_region.id)),
        )
        global_plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="global-plan",
            name="Global Plan",
        )
        plans = db_manager.list_plans(region_id=UUID(str(seed_region.id)))
        plan_ids = [str(p.id) for p in plans]
        assert str(regional_plan.id) in plan_ids
        assert str(global_plan.id) not in plan_ids

    def test_list_licenses_with_plan_filter(self, db_manager, full_seed_data):
        """Test list_licenses with plan_id filter."""
        plan = full_seed_data["plan"]
        licenses = db_manager.list_licenses(plan_id=UUID(str(plan.id)))
        assert len(licenses) >= 1
        for lic in licenses:
            assert str(lic.plan_id) == str(plan.id)

    def test_introspect_token_orphaned_license(self, db_manager, seed_license):
        """Introspect token whose license row is missing returns license_not_found.

        Simulates an orphan token by temporarily disabling FK enforcement.
        This covers the defensive code path in introspect_token.
        """

        # Create a token
        _, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        # Verify it works first
        result = db_manager.introspect_token(raw_token)
        assert result["active"] is True

        # Point the token at a non-existent license_id to simulate orphan.
        # Drop FK constraint temporarily to allow the invalid reference.
        session = db_manager.get_session()
        try:
            fake_license_id = "00000000-0000-0000-0000-000000000000"
            session.execute(
                text("ALTER TABLE token DROP CONSTRAINT IF EXISTS token_license_id_fkey")
            )
            session.execute(
                text("UPDATE token SET license_id = :fake WHERE license_id = :real"),
                {"fake": fake_license_id, "real": str(seed_license.id)},
            )
            session.commit()
        finally:
            session.close()

        # Now introspect should find token but not its license
        result = db_manager.introspect_token(raw_token)
        assert result["active"] is False
        assert result["reason"] == "license_not_found"

        # Restore the FK constraint this test dropped. Test isolation is now
        # TRUNCATE-based (one statement) rather than a per-test schema recreate,
        # so a DDL change here would otherwise leak into every later test
        # (e.g. test_token_requires_valid_license_id would stop raising). Remove
        # the orphan row first so the constraint can be re-validated.
        restore = db_manager.get_session()
        try:
            restore.execute(
                text("DELETE FROM token WHERE license_id = :fake"),
                {"fake": fake_license_id},
            )
            restore.execute(
                text(
                    "ALTER TABLE token ADD CONSTRAINT token_license_id_fkey "
                    "FOREIGN KEY (license_id) REFERENCES license(id) ON DELETE CASCADE"
                )
            )
            restore.commit()
        finally:
            restore.close()


class TestEnvBool:
    """Test _env_bool helper function."""

    def test_env_bool_true_values(self):
        """Test _env_bool returns True for various truthy values (covers line 53)."""
        import os
        from unittest.mock import patch

        import main

        for value in ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"]:
            with patch.dict(os.environ, {"TEST_FLAG": value}):
                assert main._env_bool("TEST_FLAG") is True

    def test_env_bool_false_values(self):
        """Test _env_bool returns False for falsy values."""
        import os
        from unittest.mock import patch

        import main

        for value in ["0", "false", "no", "off", "random"]:
            with patch.dict(os.environ, {"TEST_FLAG": value}):
                assert main._env_bool("TEST_FLAG") is False

    def test_env_bool_default(self):
        """Test _env_bool returns default when env var not set."""
        import os
        from unittest.mock import patch

        import main

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT_FLAG_12345", None)
            assert main._env_bool("NONEXISTENT_FLAG_12345") is False
            assert main._env_bool("NONEXISTENT_FLAG_12345", True) is True


class TestProvisionBudget:
    """Test the _provision_budget fire-and-forget function."""

    async def test_provision_budget_with_token_budget(self, db_manager, full_seed_data):
        """Covers lines 90-101: _provision_budget with token_budget_monthly."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = 10000
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/api/budget" in call_args[0][0]
            assert call_args[1]["json"]["budget_tokens"] == 10000
            # Regression guard for the duplicate-budget bug: provision MUST tag
            # the body with source='license_sync' so metering's POST handler
            # routes to the idempotent upsert instead of stacking a new row
            # next to the one sync-from-licenses also creates.
            assert call_args[1]["json"]["source"] == "license_sync"

        main.db_manager = original_db

    async def test_provision_budget_passes_license_sync_source(self, db_manager, full_seed_data):
        """Regression: _provision_budget must POST source='license_sync' so
        metering dedups against the sync-from-licenses reconciler. Without
        this the two paths each insert a row and cascade-drain doubles the
        effective entitlement (live E2E bug B2)."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = 42_000
            db_plan.quota_monthly = 0
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=201))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )

        # Two POSTs: the token budget AND the quota config. quota_monthly=0 is a
        # block-all plan and MUST be forwarded (it is no longer treated as
        # "no quota"). Locate the budget POST and assert its source dedup tag.
        assert mock_client.post.call_count == 2
        budget_calls = [c for c in mock_client.post.call_args_list if "/api/budget" in c.args[0]]
        quota_calls = [
            c for c in mock_client.post.call_args_list if "/api/quota/config" in c.args[0]
        ]
        assert len(budget_calls) == 1
        assert len(quota_calls) == 1
        body = budget_calls[0].kwargs["json"]
        assert body["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert body["license_id"] == str(full_seed_data["license"].id)
        assert body["budget_tokens"] == 42_000
        assert body["period"] == "monthly"
        assert body["source"] == "license_sync"
        # Block-all quota is forwarded as the literal 0.
        assert quota_calls[0].kwargs["json"]["quota_monthly"] == 0

        main.db_manager = original_db

    async def test_provision_budget_sends_auth_header(self, db_manager, full_seed_data):
        """Regression: licenses → metering POST /api/budget must include
        an Authorization: Bearer <jwt> header so metering-service does
        not 401. Without this, budgets silently fail to land."""
        import main
        import service_auth

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = 10000
            db_plan.quota_monthly = 5000
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        # Stub the service-token provider to return a fake JWT.
        original_provider = service_auth._provider
        fake_provider = AsyncMock()
        fake_provider.get_token = AsyncMock(return_value="fake-sa-jwt")
        service_auth._provider = fake_provider

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        try:
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await main._provision_budget(
                    license_id=str(full_seed_data["license"].id),
                    tenant_id="01000000-0000-7000-8000-000000000001",
                    plan_id=str(plan.id),
                )

            # Both /api/budget and /api/quota/config calls must carry the
            # service-account JWT in Authorization AND the tenant in
            # X-Tenant-Id (the canonical tenant carrier — see
            # procedure_management commit 16e5133 for the receiving-side
            # contract that licenses must match).
            assert mock_client.post.call_count == 2
            for call in mock_client.post.call_args_list:
                headers = call.kwargs.get("headers") or {}
                assert headers.get("Authorization") == "Bearer fake-sa-jwt", (
                    f"Expected auth header on {call.args[0]}; got headers={headers}"
                )
                assert headers.get("X-Tenant-Id") == "01000000-0000-7000-8000-000000000001", (
                    f"Expected X-Tenant-Id on {call.args[0]}; got headers={headers}"
                )
        finally:
            service_auth._provider = original_provider
            main.db_manager = original_db

    async def test_provision_budget_with_quota(self, db_manager, full_seed_data):
        """Covers lines 105-114: _provision_budget with quota_monthly."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            # NULL (not 0) is the "no token budget" signal: 0 is a block-all
            # budget and IS pushed, so it can no longer be used to isolate the
            # quota POST. See test_provision_token_budget_zero_is_block_all.
            db_plan.token_budget_monthly = None
            db_plan.quota_monthly = 5000
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/api/quota/config" in call_args[0][0]
            assert call_args[1]["json"]["quota_monthly"] == 5000
            # billing REQUIRES tenant_id in the BODY (QuotaConfigRequest); the
            # X-Tenant-Id header does not satisfy a pydantic body model.
            assert call_args[1]["json"]["tenant_id"] == "01000000-0000-7000-8000-000000000001"

        main.db_manager = original_db

    async def test_provision_budget_quota_zero_is_block_all(self, db_manager, full_seed_data):
        """quota_monthly=0 is a BLOCK-ALL (zero-allowance) plan and MUST be
        forwarded to metering as quota_monthly=0. The old ``if quota and
        quota > 0`` guard treated the falsy 0 as "no quota" and skipped the
        push, so a block-all license silently behaved as unlimited. NULL still
        means unlimited (skipped)."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = None  # NULL = unlimited = no budget push
            db_plan.quota_monthly = 0  # block-all — MUST push
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )
            # Exactly one POST — quota config with the literal 0 (block-all).
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/api/quota/config" in call_args[0][0]
            assert call_args[1]["json"]["quota_monthly"] == 0

        main.db_manager = original_db

    async def test_provision_budget_quota_null_is_unlimited_skips(self, db_manager, full_seed_data):
        """quota_monthly=NULL means unlimited and MUST NOT push a quota config
        (regression guard so the block-all fix does not forward NULL as 0)."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = None  # unlimited
            db_plan.quota_monthly = None  # unlimited
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )
            # No pushes at all: token budget 0 and quota NULL.
            mock_client.post.assert_not_called()

        main.db_manager = original_db

    async def _run_provision(
        self,
        db_manager,
        full_seed_data,
        *,
        token_budget_monthly,
        quota_monthly,
        tenant_id="01000000-0000-7000-8000-000000000001",
        status_code=200,
    ):
        """Drive ``_provision_budget`` with a given plan shape; return the mock.

        Shared by the contract-guard tests below so each one asserts on the
        REAL outgoing bodies rather than a hand-rolled approximation of them.
        """
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = token_budget_monthly
            db_plan.quota_monthly = quota_monthly
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=status_code))
        try:
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await main._provision_budget(
                    license_id=str(full_seed_data["license"].id),
                    tenant_id=tenant_id,
                    plan_id=str(plan.id),
                )
        finally:
            main.db_manager = original_db
        return mock_client

    @staticmethod
    def _bodies(mock_client, path):
        return [c.kwargs["json"] for c in mock_client.post.call_args_list if path in c.args[0]]

    async def test_provision_quota_body_carries_tenant_id(self, db_manager, full_seed_data):
        """Regression (P0): the /api/quota/config BODY must carry ``tenant_id``.

        billing's ``QuotaConfigRequest`` declares ``tenant_id: str = Field(
        min_length=1)`` — a REQUIRED body field. FastAPI never reads a header
        into a body model, so sending the tenant only as ``X-Tenant-Id`` made
        every single quota registration 422 (both retry attempts burnt on a
        deterministic validation error), leaving no billing config row, hence
        no ``quota:limit:{id}`` in Redis, hence license-authz finding no config
        and ALLOWING: request-quota enforcement silently dark for the whole
        direct-create path.
        """
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=None, quota_monthly=5000
        )
        quota_bodies = self._bodies(mock_client, "/api/quota/config")
        assert len(quota_bodies) == 1
        assert quota_bodies[0]["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert quota_bodies[0]["license_id"] == str(full_seed_data["license"].id)
        assert quota_bodies[0]["quota_monthly"] == 5000

    async def test_provision_quota_body_satisfies_billing_contract(
        self, db_manager, full_seed_data
    ):
        """Contract guard: the outgoing quota body must satisfy billing's real
        ``QuotaConfigRequest`` schema, not just the subset this suite happens to
        assert. A field-by-field assertion would have gone stale the moment
        billing added ``tenant_id``; the guard is what makes the NEXT added
        field fail here instead of in production.

        Schemas are vendored (``tests/support/billing_contract.json``) so this
        can never skip itself green — see that module's docstring.

        The ``len(bodies) == 1`` precondition is load-bearing, not decoration:
        without it the assertion lives inside a ``for`` whose iterable is empty
        whenever the quota push is skipped, so the test reports GREEN having
        checked nothing. Measured: neutering the quota guard to
        ``if False and ...`` left THIS test green while seven siblings went red.
        """
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=None, quota_monthly=5000
        )
        bodies = self._bodies(mock_client, "/api/quota/config")
        assert len(bodies) == 1, f"quota body must be POSTed exactly once, got {bodies}"
        assert_satisfies("QuotaConfigRequest", bodies[0])

    async def test_provision_quota_body_satisfies_contract_for_block_all(
        self, db_manager, full_seed_data
    ):
        """Same guard on the block-all (0) plan — billing accepts ``ge=0``."""
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=None, quota_monthly=0
        )
        bodies = self._bodies(mock_client, "/api/quota/config")
        assert len(bodies) == 1
        assert bodies[0]["quota_monthly"] == 0
        assert_satisfies("QuotaConfigRequest", bodies[0])

    async def test_provision_budget_body_satisfies_billing_contract(
        self, db_manager, full_seed_data
    ):
        """Contract guard on the sibling /api/budget body (``TokenBudgetCreate``)."""
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=42_000, quota_monthly=None
        )
        bodies = self._bodies(mock_client, "/api/budget")
        assert len(bodies) == 1
        assert_satisfies("TokenBudgetCreate", bodies[0])

    async def test_provision_token_budget_zero_is_block_all(self, db_manager, full_seed_data):
        """``token_budget_monthly = 0`` is a BLOCK-ALL budget and MUST be pushed.

        The old ``if plan.token_budget_monthly and ... > 0`` guard dropped the
        falsy 0, so a zero-allowance plan silently provisioned NO budget at all
        and therefore behaved as UNLIMITED — the identical defect already fixed
        on the sibling quota guard. NULL (absent) remains the unlimited signal.
        """
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=0, quota_monthly=None
        )
        bodies = self._bodies(mock_client, "/api/budget")
        assert len(bodies) == 1, f"block-all budget must be pushed exactly once, got {bodies}"
        assert bodies[0]["budget_tokens"] == 0
        assert bodies[0]["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert bodies[0]["source"] == "license_sync"

    async def test_provision_token_budget_null_pushes_nothing(self, db_manager, full_seed_data):
        """Over-correction guard: NULL token budget = unlimited = NO budget POST."""
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=None, quota_monthly=None
        )
        assert self._bodies(mock_client, "/api/budget") == []
        mock_client.post.assert_not_called()

    async def test_zero_budget_body_is_fully_contract_valid(self, db_manager, full_seed_data):
        """The block-all budget body now satisfies billing's contract outright.

        This replaces ``test_zero_budget_body_only_breaches_billings_gt0_floor``,
        which was a deliberate PAWL: it asserted that the single acceptable
        breach was billing's not-yet-relaxed ``gt=0`` floor, and said in its own
        docstring that an EMPTY violation list means billing relaxed it — delete
        the carve-out. billing #73 did exactly that (``Field(gt=0)`` → ``ge=0``,
        so a block-all budget is representable at all), the vendored contract has
        been refreshed to that commit, and the pawl fired. This is the carve-out
        being deleted, not weakened: the assertion got STRICTER (zero violations,
        not one specific tolerated one).

        Confirmed against the DEPLOYED billing pod rather than only its repo:
        the served OpenAPI declares ``budget_tokens`` as
        ``{"type": "integer", "minimum": 0.0}``.
        """
        mock_client = await self._run_provision(
            db_manager, full_seed_data, token_budget_monthly=0, quota_monthly=None
        )
        bodies = self._bodies(mock_client, "/api/budget")
        assert len(bodies) == 1, "a block-all budget must still be PUSHED, not skipped"
        assert violations("TokenBudgetCreate", bodies[0]) == [], (
            "the block-all budget body must now be fully valid against billing's "
            "contract; any violation here is a real defect, not a tolerated floor"
        )

    # NOTE: `test_provision_budget_plan_not_found` used to sit here with ZERO
    # assertions — it awaited _provision_budget and returned. It could not fail:
    # the function's blanket `except Exception` means "does not raise" is true by
    # construction. The same scenario is covered WITH assertions by
    # tests/unit/test_metering_durability.py::
    # test_missing_plan_does_not_record_and_logs_loudly, so it is deleted rather
    # than given assertions that would duplicate it.

    async def _seed_plan_budget(self, db_manager, plan, **columns):
        """Set plan budget columns and return, so each test states only what it isolates."""
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            for name, value in columns.items():
                setattr(db_plan, name, value)
            session.commit()
        finally:
            session.close()

    async def test_provision_budget_transport_error_is_retried_then_logged_loudly(
        self, db_manager, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """A TRANSPORT fault on the budget push must be retried, then reported.

        This test used to inject the BUILTIN ``ConnectionError``, which descends from
        ``OSError``. Production catches ``httpx.HTTPError``. The two trees are disjoint,
        so the injected error sailed past the retry handler entirely and was swallowed
        by the blanket ``except Exception`` at the end of ``_provision_budget`` — the
        retry loop never ran, and the test asserted nothing at all, so nobody noticed.
        Adding assertions alone would NOT have closed it: ``call_count == 2`` fails
        under the builtin error. The injected TYPE had to change first.
        """
        import main
        import service_auth

        plan = full_seed_data["plan"]
        # quota NULL = unlimited, so only the budget loop fires.
        await self._seed_plan_budget(
            db_manager, plan, token_budget_monthly=5000, quota_monthly=None
        )

        original_db = main.db_manager
        main.db_manager = db_manager
        # Pin the service-token provider: with SERVICE_CLIENT_ID/SECRET exported in the
        # environment this call would take a DIFFERENT path and never attempt the push,
        # making the test silently weaker on some machines than others.
        original_provider = service_auth._provider
        fake_provider = AsyncMock()
        fake_provider.get_token = AsyncMock(return_value="fake-sa-jwt")
        service_auth._provider = fake_provider

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        try:
            with (
                patch("httpx.AsyncClient") as mock_cls,
                patch("main.asyncio.sleep", new_callable=AsyncMock),
                caplog.at_level(logging.ERROR, logger="main"),
            ):
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                # Fire-and-forget: must not raise into the license-creation response.
                await main._provision_budget(
                    license_id=str(full_seed_data["license"].id),
                    tenant_id="01000000-0000-7000-8000-000000000001",
                    plan_id=str(plan.id),
                )
        finally:
            service_auth._provider = original_provider
            main.db_manager = original_db

        assert mock_client.post.call_count == 2, (
            "a transport error must still get its second attempt — this is the "
            "assertion the builtin-ConnectionError version could not make"
        )
        loud = [r for r in caplog.records if main.METERING_FAILURE_MARKER in r.getMessage()]
        assert loud, (
            "an unprovisioned budget must be reported loudly — the licence is unmetered; "
            f"got {[r.getMessage() for r in caplog.records]}"
        )

    async def test_provision_quota_transport_error_is_retried_then_logged_loudly(
        self, db_manager, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """Same defect, quota loop. See the sibling above for why the type mattered."""
        import main
        import service_auth

        plan = full_seed_data["plan"]
        # token budget NULL = unlimited, so only the quota loop fires.
        await self._seed_plan_budget(
            db_manager, plan, token_budget_monthly=None, quota_monthly=3000
        )

        original_db = main.db_manager
        main.db_manager = db_manager
        original_provider = service_auth._provider
        fake_provider = AsyncMock()
        fake_provider.get_token = AsyncMock(return_value="fake-sa-jwt")
        service_auth._provider = fake_provider

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        try:
            with (
                patch("httpx.AsyncClient") as mock_cls,
                patch("main.asyncio.sleep", new_callable=AsyncMock),
                caplog.at_level(logging.ERROR, logger="main"),
            ):
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await main._provision_budget(
                    license_id=str(full_seed_data["license"].id),
                    tenant_id="01000000-0000-7000-8000-000000000001",
                    plan_id=str(plan.id),
                )
        finally:
            service_auth._provider = original_provider
            main.db_manager = original_db

        assert mock_client.post.call_count == 2, (
            "a transport error on the quota push must still get its second attempt"
        )
        loud = [r for r in caplog.records if main.METERING_FAILURE_MARKER in r.getMessage()]
        assert loud, f"expected a loud ERROR; got {[r.getMessage() for r in caplog.records]}"

    async def test_the_builtin_connectionerror_is_not_what_production_catches(self):
        """Pin the fact that made both tests above vacuous for months.

        If these ever become the same tree — or someone 'simplifies' the injected type
        back to the builtin — this says so directly instead of leaving a green test that
        exercises the blanket handler by accident.
        """
        assert not issubclass(ConnectionError, httpx.HTTPError)
        assert issubclass(httpx.ConnectError, httpx.HTTPError)

    async def test_provision_budget_non2xx_is_retried_not_silently_accepted(
        self, db_manager, full_seed_data
    ):
        """A non-2xx metering response (e.g. 503 during a rollout) must be RETRIED,
        not silently treated as success.

        httpx does not raise on 4xx/5xx, so the old code ``break``ed after the
        first non-exception response — a 503/402/500 was accepted as done, with
        no retry and no log, dropping the tenant's budget provisioning on the
        floor. Regression: the POST is attempted twice (initial + one retry).
        """
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            db_plan.token_budget_monthly = 7000
            # quota NULL = unlimited so ONLY the budget loop fires — this test
            # isolates the budget-retry behaviour. (0 would now also push a
            # block-all quota; the quota loop is covered by the sibling test.)
            db_plan.quota_monthly = None
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        # Metering answers 503 both times (never recovers within the retry).
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=503, text="unavailable"))

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # Must not raise (fire-and-forget), but must have retried.
            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )

        assert mock_client.post.call_count == 2, (
            "non-2xx budget response must be retried, not silently accepted"
        )
        main.db_manager = original_db

    async def test_provision_quota_non2xx_is_retried_not_silently_accepted(
        self, db_manager, full_seed_data
    ):
        """Same as the budget case, for the /api/quota/config POST loop."""
        import main

        plan = full_seed_data["plan"]
        session = db_manager.get_session()
        try:
            from models import Plan

            db_plan = session.query(Plan).filter(Plan.id == plan.id).first()
            assert db_plan is not None
            # NULL = unlimited so ONLY the quota loop fires — this test isolates
            # the quota-retry behaviour. (0 would now also push a block-all
            # budget; the budget loop is covered by the sibling test.)
            db_plan.token_budget_monthly = None
            db_plan.quota_monthly = 2500
            session.commit()
        finally:
            session.close()

        original_db = main.db_manager
        main.db_manager = db_manager

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=500, text="boom"))

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await main._provision_budget(
                license_id=str(full_seed_data["license"].id),
                tenant_id="01000000-0000-7000-8000-000000000001",
                plan_id=str(plan.id),
            )

        assert mock_client.post.call_count == 2, (
            "non-2xx quota response must be retried, not silently accepted"
        )
        main.db_manager = original_db


class TestTenantConstraints:
    """Test the Tenant table's DB constraints (name format + uniqueness).

    The canonical (UUIDv7, name) registry remains; the public name→UUID lookup
    endpoint (GET /license/tenants/{name}) was retired once tenant identity
    became the canonical UUID end-to-end (no consumer re-resolves by name)."""

    def test_create_tenant_rejects_invalid_name(self, db_manager):
        """The DB CheckConstraint rejects uppercase / space / special-char names."""
        import pytest as _pytest
        from sqlalchemy.exc import IntegrityError

        with _pytest.raises(IntegrityError):
            db_manager.create_tenant(name="Invalid Tenant", owner_key="platform")

    def test_create_tenant_rejects_duplicate_name(self, db_manager):
        """The unique constraint on name rejects duplicates."""
        import pytest as _pytest
        from sqlalchemy.exc import IntegrityError

        db_manager.create_tenant(name="dup-tenant", owner_key="platform")
        with _pytest.raises(IntegrityError):
            db_manager.create_tenant(name="dup-tenant", owner_key="platform")

    def test_ensure_tenant_registers_with_explicit_id(self, db_manager):
        """ensure_tenant registers a tenants row whose id EQUALS the supplied
        UUID (unlike create_tenant, which mints a random uuid7)."""
        tid = "019e00dd-0000-7000-8000-000000000123"
        t = db_manager.ensure_tenant(tid, name="explicit-id-tenant", owner_key="platform")
        assert str(t.id) == tid
        assert db_manager.get_tenant(tid) is not None

    def test_ensure_tenant_is_idempotent(self, db_manager):
        """Re-registering the same id returns the existing row (no duplicate,
        no error) even if a different name is passed."""
        tid = "019e00dd-0000-7000-8000-000000000456"
        first = db_manager.ensure_tenant(tid, name="idem-tenant", owner_key="platform")
        second = db_manager.ensure_tenant(tid, name="idem-tenant-renamed", owner_key="platform")
        assert str(second.id) == str(first.id) == tid
        # The first name is kept — an id match wins over the new name.
        assert second.name == "idem-tenant"

    def test_ensure_tenant_rejects_name_owned_by_other_tenant(self, db_manager):
        """A name already owned by a DIFFERENT tenant id raises ValueError, so a
        typo can never silently repoint a license's tenant."""
        import pytest as _pytest

        db_manager.ensure_tenant(
            "019e00dd-0000-7000-8000-000000000789", name="taken-name", owner_key="platform"
        )
        with _pytest.raises(ValueError, match="already belongs to"):
            db_manager.ensure_tenant(
                "019e00dd-0000-7000-8000-0000000007ff", name="taken-name", owner_key="platform"
            )

    # NB: a positive "removes orphan rows" test is no longer possible — since
    # migration 016 added the license.tenant_id -> tenants.id FK, an orphan
    # (a license whose tenant_id has no tenants row) can no longer be created via
    # the ORM (the insert FK-violates; ON DELETE RESTRICT also blocks orphaning by
    # deleting the tenant). delete_orphan_licenses_by_tenant is retained as a
    # guarded historical-cleanup tool; only its guard is testable now.

    def test_delete_orphan_licenses_refuses_registered_tenant(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """Guard: the delete refuses when the tenant_id IS registered — it may
        only ever remove genuine orphans, never a real tenant's licenses."""
        import pytest as _pytest

        registered_tid = "019e00ee-0000-7000-8000-000000000002"
        db_manager.ensure_tenant(registered_tid, name="real-tenant-keep", owner_key="platform")
        db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id=registered_tid,
            status="active",
        )
        with _pytest.raises(ValueError, match="IS a registered tenant"):
            db_manager.delete_orphan_licenses_by_tenant(registered_tid)
        # The license survives the refused delete.
        assert len(db_manager.list_licenses(tenant_id=registered_tid)) == 1
