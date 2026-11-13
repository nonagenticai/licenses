"""Tests for per-product offerings (usage-policy templates)."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID

import pytest

from models import Tenant
from tests.support.billing_contract import assert_satisfies

SEED_TENANT = "01000000-0000-7000-8000-000000000001"  # matches auth_client tenant header
#: Repo root, for the static censuses in this module (see TestKnownUnfixedRetryLoops).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_tenant(db_manager, *, byo_mode="local_allowed", tenant_id=SEED_TENANT, name="prov-test"):
    session = db_manager.get_session()
    try:
        # merge (upsert by PK), not add: the auth_client fixture already registers
        # the client's SEED_TENANT to satisfy the license-create mint guard, so a
        # raw insert of the same id would hit tenants_pkey. merge sets byo_mode.
        session.merge(
            Tenant(id=UUID(tenant_id), name=name, byo_mode=byo_mode, owner_key="platform")
        )
        session.commit()
    finally:
        session.close()


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, resp, capture: dict) -> None:
        self._resp = resp
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        self._capture.update(url=url, json=json, headers=headers)
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


class _SequenceClient:
    """Like ``_FakeClient`` but records EVERY call and can answer differently per call.

    ``_FakeClient`` holds a SINGLE response and replays it forever, so a test
    built on it structurally cannot observe how many attempts the helper made —
    which is exactly why "a 403 is retried" survived ``test_non_2xx_returns_false``
    for as long as it did. This one appends to ``calls`` and pops from a scripted
    sequence, so "how many times did we hit billing" is an assertable fact.

    An entry may be a ``_FakeResp`` (returned) or an ``Exception`` (raised). The
    last entry is reused if the helper asks for more than were scripted, so an
    over-eager retry shows up as a call COUNT, never as an IndexError that would
    be mistaken for a different defect.
    """

    def __init__(self, responses, calls: list) -> None:
        self._responses = list(responses)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        index = min(len(self.calls), len(self._responses) - 1)
        self.calls.append({"url": url, "json": json, "headers": headers})
        answer = self._responses[index]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _no_sleep(monkeypatch) -> list[float]:
    """Replace ``main.asyncio.sleep`` with a recorder. Returns the recorded delays.

    Two jobs: the suite must not pay a real second per retry case, and the number
    of sleeps is a SECOND independent signal about the retry decision — a helper
    that stopped retrying but still slept would be caught here even if the call
    count somehow matched.
    """
    import main

    slept: list[float] = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", _record)
    return slept


class _FakeProvider:
    async def get_token(self) -> str:
        return "svc-token"


class TestCreateOffering:
    def test_create_returns_dict(self, db_manager, seed_app):
        result = db_manager.create_offering(
            seed_app.key,
            "free-high-markup",
            "Free access, high markup",
            "byo_required",
            usage={"markup_pct": 50},
        )
        assert result is not None
        assert result["app_key"] == seed_app.key
        assert result["code"] == "free-high-markup"
        assert result["byo_mode"] == "byo_required"
        assert result["markup_pct"] == 50.0

    def test_usage_fields_persist(self, db_manager, seed_app):
        result = db_manager.create_offering(
            seed_app.key,
            "surcharged",
            "Surcharge-only",
            "local_allowed",
            usage={
                "surcharge_input_per_1m_usd": 1.25,
                "surcharge_per_request_usd": 0.002,
            },
        )
        assert result is not None
        assert result["surcharge_input_per_1m_usd"] == 1.25
        assert result["surcharge_per_request_usd"] == 0.002
        # Unset usage fields default to None.
        assert result["markup_pct"] is None
        assert result["surcharge_output_per_1m_usd"] is None

    def test_unknown_app_returns_none(self, db_manager):
        assert db_manager.create_offering("no-such-app", "x", "X", "byo_required") is None

    def test_invalid_byo_mode_raises(self, db_manager, seed_app):
        with pytest.raises(ValueError, match="invalid byo_mode"):
            db_manager.create_offering(seed_app.key, "x", "X", "bogus")

    def test_upsert_updates_existing(self, db_manager, seed_app):
        db_manager.create_offering(
            seed_app.key, "plan-a", "Plan A", "byo_required", usage={"markup_pct": 20}
        )
        updated = db_manager.create_offering(
            seed_app.key, "plan-a", "Plan A v2", "local_allowed", usage={"markup_pct": 10}
        )
        assert updated is not None
        assert updated["name"] == "Plan A v2"
        assert updated["byo_mode"] == "local_allowed"
        assert updated["markup_pct"] == 10.0
        # Still a single row for (app, code).
        assert len(db_manager.list_offerings(app_key=seed_app.key)) == 1


class TestReadOffering:
    def test_get_offering(self, db_manager, seed_app):
        db_manager.create_offering(seed_app.key, "c1", "C1", "byo_required")
        result = db_manager.get_offering(seed_app.key, "c1")
        assert result is not None and result["code"] == "c1"

    def test_get_missing_returns_none(self, db_manager, seed_app):
        assert db_manager.get_offering(seed_app.key, "missing") is None
        assert db_manager.get_offering("no-such-app", "c1") is None

    def test_list_all_and_filtered(self, db_manager, seed_app):
        db_manager.create_offering(seed_app.key, "c1", "C1", "byo_required")
        db_manager.create_offering(seed_app.key, "c2", "C2", "local_allowed")
        assert len(db_manager.list_offerings()) == 2
        assert len(db_manager.list_offerings(app_key=seed_app.key)) == 2
        assert db_manager.list_offerings(app_key="no-such-app") == []


class TestDeleteOffering:
    def test_delete_removes_row(self, db_manager, seed_app):
        db_manager.create_offering(seed_app.key, "c1", "C1", "byo_required")
        assert db_manager.delete_offering(seed_app.key, "c1") is True
        assert db_manager.get_offering(seed_app.key, "c1") is None

    def test_delete_missing_returns_false(self, db_manager, seed_app):
        assert db_manager.delete_offering(seed_app.key, "missing") is False
        assert db_manager.delete_offering("no-such-app", "c1") is False


class TestOfferingRoutes:
    """HTTP surface (license-admin gated)."""

    def test_post_creates_offering(self, auth_client_admin, seed_app):
        resp = auth_client_admin.post(
            "/offerings",
            json={
                "app_key": seed_app.key,
                "code": "free-high-markup",
                "name": "Free / high markup",
                "byo_mode": "byo_required",
                "markup_pct": 50,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == "free-high-markup"
        assert body["markup_pct"] == 50.0

    def test_post_unknown_app_404(self, auth_client_admin):
        resp = auth_client_admin.post(
            "/offerings",
            json={"app_key": "no-such-app", "code": "x", "name": "X", "byo_mode": "byo_required"},
        )
        assert resp.status_code == 404

    def test_post_invalid_byo_mode_422(self, auth_client_admin, seed_app):
        resp = auth_client_admin.post(
            "/offerings",
            json={"app_key": seed_app.key, "code": "x", "name": "X", "byo_mode": "bogus"},
        )
        assert resp.status_code == 422

    def test_list_and_get_and_delete(self, auth_client_admin, seed_app):
        auth_client_admin.post(
            "/offerings",
            json={"app_key": seed_app.key, "code": "c1", "name": "C1", "byo_mode": "byo_required"},
        )
        listed = auth_client_admin.get("/offerings", params={"app": seed_app.key})
        assert listed.status_code == 200
        assert len(listed.json()["offerings"]) == 1

        got = auth_client_admin.get(f"/offerings/{seed_app.key}/c1")
        assert got.status_code == 200 and got.json()["code"] == "c1"

        missing = auth_client_admin.get(f"/offerings/{seed_app.key}/nope")
        assert missing.status_code == 404

        deleted = auth_client_admin.delete(f"/offerings/{seed_app.key}/c1")
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True

    def test_requires_admin_role(self, auth_client_reader, seed_app):
        resp = auth_client_reader.post(
            "/offerings",
            json={"app_key": seed_app.key, "code": "c1", "name": "C1", "byo_mode": "byo_required"},
        )
        assert resp.status_code in (401, 403)


class TestProvisionMarkupHelper:
    """The S2S billing markup-rule write (`_provision_offering_markup`)."""

    async def test_unset_url_returns_none(self, monkeypatch):
        """No URL means billing was never reached — ``None``, not a status code."""
        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "")
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 50.0}) is None

    async def test_posts_correct_body_on_success(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        capture: dict = {}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(201), capture))

        status_code = await main._provision_offering_markup(
            "tid-1", "w2v-image", {"markup_pct": 50.0}
        )
        assert status_code == 201
        assert capture["url"].endswith("/api/config/markup-rules")
        assert capture["json"] == {
            "tenant_id": "tid-1",
            "service_key": "w2v-image",  # pragma: allowlist secret -- product slug, not a credential
            "markup_pct": 50.0,
        }
        assert capture["headers"]["X-Tenant-Id"] == "tid-1"
        assert capture["headers"]["Authorization"] == "Bearer svc-token"
        # 422-proofing: the body licenses builds by hand must satisfy billing's
        # real MarkupRuleCreate. Nothing here imports billing's models, so a
        # required field we omit is an invisible, deterministic 422.
        assert_satisfies("MarkupRuleCreate", capture["json"])

    async def test_non_2xx_returns_the_status(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(403, "forbidden"), {})
        )
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) == 403


class TestProvisionMarkupRetryPolicy:
    """WHICH failures are worth a second attempt (`_provision_offering_markup`).

    The defect: the loop slept a second and re-POSTed on ANY non-2xx. A 403 is
    the answer billing will keep giving — retrying it doubles the log noise and
    puts a wasted second in the request path of every fr "Provision" click.

    Every check here asserts the CALL COUNT, which the single-response
    ``_FakeClient`` used by the sibling class structurally cannot observe.
    """

    def _install(self, monkeypatch, responses) -> tuple[list, list]:
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        calls: list = []
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _SequenceClient(responses, calls))
        return calls, _no_sleep(monkeypatch)

    async def test_403_is_not_retried(self, monkeypatch):
        """The live failure: no amount of sleeping turns a permission error into a 201."""
        import main

        calls, slept = self._install(monkeypatch, [_FakeResp(403, "forbidden")])
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) == 403
        assert len(calls) == 1, f"a 403 must not be retried, got {len(calls)} attempts"
        assert slept == [], f"slept {slept} on a non-retryable status"

    async def test_404_and_422_are_not_retried(self, monkeypatch):
        """Not a 403 carve-out: any deterministic client error stops after one try."""
        import main

        for code in (400, 404, 422):
            calls, slept = self._install(monkeypatch, [_FakeResp(code, "nope")])
            assert (
                await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) == code
            )
            assert len(calls) == 1, f"HTTP {code} was retried"
            assert slept == []

    async def test_503_then_201_succeeds_on_the_second_attempt(self, monkeypatch):
        """The half that must NOT regress: a genuine blip is still retried."""
        import main

        calls, slept = self._install(
            monkeypatch, [_FakeResp(503, "rolling out"), _FakeResp(201, "")]
        )
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) == 201
        assert len(calls) == 2
        assert slept == [1]

    async def test_persistent_503_reports_the_status_after_two_attempts(self, monkeypatch):
        import main

        calls, _ = self._install(monkeypatch, [_FakeResp(503, "still down")])
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) == 503
        assert len(calls) == 2

    async def test_transport_error_is_still_retried(self, monkeypatch):
        """A connection blip is not a verdict from billing — it keeps its retry."""
        import httpx

        import main

        calls, slept = self._install(monkeypatch, [httpx.ConnectError("boom")])
        assert await main._provision_offering_markup("t", "w2v-image", {"markup_pct": 5.0}) is None
        assert len(calls) == 2, "a transport error must still get its second attempt"
        assert slept == [1]


class TestTenantOfferingReadBack:
    """`GET /tenants/{n}/offerings` — what a tenant has been provisioned.

    Before migration 025 this could not be answered AT ALL. Provisioning writes
    its effects to two stores — the posture row here and billing's markup rule +
    access charge (S2S) — and the offering's own code was persisted in neither.
    No table referenced ``offering.id``. So these tests pin the thing that was
    genuinely missing: the provenance, not the posture.
    """

    def _seed_offering(self, db_manager, seed_app, code="free-high-markup"):
        return db_manager.create_offering(
            seed_app.key, code, "Free / high markup", "byo_required", usage={}
        )

    def test_tenant_with_no_overrides_is_empty_not_404(self, auth_client_admin, db_manager):
        # [] and 404 mean different things: "no per-product overrides" vs "no such
        # tenant". Collapsing them would have an operator hunting a missing tenant.
        _make_tenant(db_manager)

        resp = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings")

        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_unknown_tenant_is_404(self, auth_client_admin):
        resp = auth_client_admin.get("/tenants/01000000-0000-7000-8000-0000000000ff/offerings")
        assert resp.status_code == 404

    def test_provisioning_records_which_offering_produced_the_posture(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        # The whole point of 025. Without it the row says 'byo_required' and
        # nothing about WHY, which is what an operator actually asks.
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app)

        async def _fake_markup(*a, **k):
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _fake_markup)
        assert (
            auth_client_admin.post(
                f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
            ).status_code
            == 200
        )

        rows = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()

        assert len(rows) == 1
        assert rows[0]["app_key"] == seed_app.key
        assert rows[0]["byo_mode"] == "byo_required"
        assert rows[0]["offering_code"] == "free-high-markup"

    def test_a_direct_posture_write_clears_a_stale_offering_code(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        # Setting the posture by hand did NOT come from an offering. Leaving the
        # previous code in place would attribute the new posture to an offering
        # that no longer explains it — a provenance field that lies is worse than
        # one that is absent.
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app)

        async def _fake_markup(*a, **k):
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _fake_markup)
        auth_client_admin.post(f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup")
        assert (
            auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()[0]["offering_code"]
            == "free-high-markup"
        )

        resp = auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "platform_only"},
        )
        assert resp.status_code == 200, resp.text

        rows = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()
        assert rows[0]["byo_mode"] == "platform_only"
        assert rows[0]["offering_code"] is None

    def test_foreign_tenant_is_404_for_a_reader(
        self, auth_client_admin, auth_client_reader, db_manager
    ):
        """The enumeration leak closed for `get_tenant` on 2026-07-27, here too.

        Without the own-tenant check this route hands ANY `license-reader` the
        per-product posture of ANY tenant — which products a customer is on, and
        under what commercial posture. 404 not 403: a 403 confirms the tenant
        exists, which is the leak itself.

        Every other test in this class authenticates as ADMIN, so deleting the
        scoping branch leaves them all green. This is the one that fails.
        """
        auth_client_admin.post("/tenants", json={"name": "acme-corp"})

        assert auth_client_reader.get("/tenants/acme-corp/offerings").status_code == 404

    def test_reader_may_read_its_own_tenant(self, auth_client_reader, db_manager):
        """Positive control: the scoping must not refuse everyone.

        Without this, an unconditional 404 would satisfy the test above and the
        gate would be indistinguishable from a broken route.
        """
        _make_tenant(db_manager)

        resp = auth_client_reader.get(f"/tenants/{SEED_TENANT}/offerings")

        assert resp.status_code == 200, resp.text

    def test_null_offering_code_is_reported_not_hidden(
        self, auth_client_admin, db_manager, seed_app
    ):
        # A row whose provenance predates 025. It must still be LISTED — dropping
        # unprovenanced rows would under-report what the tenant actually carries.
        _make_tenant(db_manager)
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "byo_required")

        rows = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()

        assert len(rows) == 1
        assert rows[0]["offering_code"] is None
        assert rows[0]["byo_mode"] == "byo_required"


class TestProvisionOfferingRoutes:
    """Apply/unapply an offering onto a tenant (posture + billing markup)."""

    def _seed_offering(self, db_manager, seed_app, *, byo_mode="byo_required", usage=None):
        return db_manager.create_offering(
            seed_app.key, "free-high-markup", "Free / high markup", byo_mode, usage=usage or {}
        )

    def test_provision_writes_posture_and_markup(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _fake_markup(tenant_id, service_key, usage):
            assert service_key == seed_app.key
            assert usage == {"markup_pct": 50.0}
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _fake_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["byo_mode"] == "byo_required"
        assert body["markup_rule_provisioned"] is True
        # Posture override written; tenant-wide untouched.
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "byo_required"
        assert db_manager._lookup_byo_mode(SEED_TENANT) == "local_allowed"

    def test_posture_only_offering_skips_markup(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app, byo_mode="platform_only", usage=None)

        called = {"n": 0}

        async def _fake_markup(*a, **k):
            called["n"] += 1
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _fake_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 200
        assert resp.json()["markup_rule_provisioned"] is False
        assert called["n"] == 0  # no usage policy → billing not called
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "platform_only"

    def test_billing_failure_rolls_back_posture(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _fail_markup(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _fail_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 502
        # Posture override rolled back → reverts to tenant-wide.
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"

    def test_billing_failure_detail_names_the_write_and_the_status(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """A bare "Failed to write billing markup rule" tells the operator nothing.

        The live failure is a 403 from billing's ``require_pricing_writer``; a 502
        that does not carry the status sends whoever clicked Provision to the pod
        logs to learn whether it was a permission problem or an outage.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _fail_markup(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _fail_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "403" in detail, detail
        assert "markup" in detail.lower(), detail
        # Never the response BODY — that is billing's text, and echoing it here
        # is the next leak (see tests/unit/test_no_exception_leak.py).
        assert "forbidden" not in detail.lower(), detail

    def test_unreachable_billing_detail_has_a_static_fallback(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """``None`` means we never got an answer — the detail must not read "HTTP None"."""
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _unreachable(*a, **k):
            return None

        monkeypatch.setattr("main._provision_offering_markup", _unreachable)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "None" not in detail, detail
        assert "markup" in detail.lower(), detail

    def test_markup_and_access_failure_details_differ(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """Two independent billing writes, two distinguishable 502s.

        With one shared message an operator cannot tell which of the two POSTs
        billing refused — and they are gated identically today only by accident.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key,
            "paid-managed",
            "Paid managed",
            "byo_required",
            usage={"markup_pct": 20},
            access_price_usd=49.0,
        )

        async def _fail(*a, **k):
            return 403

        async def _ok(*a, **k):
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _fail)
        markup_detail = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        ).json()["detail"]

        monkeypatch.setattr("main._provision_offering_markup", _ok)
        monkeypatch.setattr("main._provision_access_charge", _fail)
        access_detail = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        ).json()["detail"]

        assert markup_detail != access_detail, markup_detail
        assert "access" in access_detail.lower(), access_detail
        assert "403" in access_detail, access_detail

    def test_rollback_restores_a_preexisting_override_markup_arm(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """DATA DESTRUCTION: the rollback used to DELETE, not restore.

        ``set_tenant_app_byo_mode`` is an UPSERT, so a tenant that already carried
        a per-app posture override loses it permanently the first time a
        re-provision fails on the billing hop — which, since billing #74, is
        every single provision attempt.

        ``test_billing_failure_rolls_back_posture`` cannot see this: it starts
        with NO override, where "delete" and "restore nothing" are the same
        outcome. That is why the defect survived it.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        # Pre-existing, deliberately set, per-product posture — the value at risk.
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _fail_markup(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _fail_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 502
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "platform_only", (
            "the tenant's pre-existing per-app posture override was destroyed by a "
            "failed re-provision instead of being restored"
        )

    def test_rollback_restores_the_prior_offering_code_not_just_the_posture(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """Restoring the posture alone leaves a FAILED provision's provenance.

        The two rollback tests above assert only `_lookup_byo_mode`, and their
        fixture seeds the prior row with `offering_code` already None — so they
        cannot tell a restored code from a cleared one. Both stay green if the
        rollback drops the provenance.

        Concretely: a tenant on `paid-managed` is re-provisioned onto
        `free-high-markup`, the billing hop 502s, and the row comes back with the
        right posture but NO offering code. `GET /tenants/{n}/offerings` then
        reports "provenance unknown" for a tenant that IS provisioned — the
        column reading as a lie rather than as an absence.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        # A prior posture that CAME FROM an offering. The value at risk is the
        # code, so it must differ from the incoming one.
        db_manager.set_tenant_app_byo_mode(
            SEED_TENANT, seed_app.key, "platform_only", offering_code="paid-managed"
        )
        self._seed_offering(db_manager, seed_app, byo_mode="byo_required", usage={"markup_pct": 50})

        async def _fail_markup(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _fail_markup)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 502

        rows = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()
        assert rows[0]["byo_mode"] == "platform_only"
        assert rows[0]["offering_code"] == "paid-managed", (
            "the failed provision left the tenant's provenance cleared (or set to "
            "the offering that never applied) instead of restoring the prior code"
        )

    def test_rollback_restores_a_preexisting_override_access_arm(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """The same defect on the ACCESS rollback site — a distinct code path."""
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "platform_only")
        db_manager.create_offering(
            seed_app.key,
            "paid-managed",
            "Paid managed",
            "byo_required",
            usage={"markup_pct": 20},
            access_price_usd=49.0,
        )

        async def _ok_markup(*a, **k):
            return 201

        async def _fail_access(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _fail_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        )
        assert resp.status_code == 502
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "platform_only", (
            "the ACCESS rollback arm destroyed the tenant's pre-existing per-app posture"
        )

    def test_access_failure_detail_admits_the_markup_rule_survives(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """The rollback is PARTIAL and the 502 must not pretend otherwise.

        When the access-charge write fails after the markup rule was accepted,
        that markup rule stays — billing exposes no DELETE on
        ``/api/config/markup-rules``, and it resolves markups by
        ``(tenant, service_key, ...)`` independently of this service's posture.
        So the tenant is billed at a markup for a provision that reported
        failure. An earlier revision of this endpoint said "provisioning rolled
        back" here, which sent the operator looking for nothing to clean up.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key,
            "paid-managed",
            "Paid managed",
            "byo_required",
            usage={"markup_pct": 20},
            access_price_usd=49.0,
        )

        async def _ok_markup(*a, **k):
            return 201

        async def _fail_access(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _fail_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        )
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "markup rule" in detail.lower(), detail
        assert "not removed" in detail.lower(), detail
        # The false claim specifically.
        assert "provisioning rolled back" not in detail.lower(), detail

    def test_access_failure_without_a_markup_write_makes_no_residue_claim(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        """The other half: no markup rule was written, so none survives.

        Without this, "the markup rule was not removed" would be asserted on an
        offering that never had one — a note that is itself misinformation, and
        the mirror image of the defect above.
        """
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key,
            "free-priced",
            "Free of markup, priced access",
            "byo_required",
            access_price_usd=49.0,
        )

        called = {"markup": 0}

        async def _markup(*a, **k):
            called["markup"] += 1
            return 201

        async def _fail_access(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _markup)
        monkeypatch.setattr("main._provision_access_charge", _fail_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-priced"
        )
        assert resp.status_code == 502
        assert called["markup"] == 0, "no usage policy → the markup write must not happen"
        detail = resp.json()["detail"]
        assert "not removed" not in detail.lower(), detail
        assert "403" in detail, detail

    def test_provision_unknown_offering_404(self, auth_client_admin, db_manager, seed_app):
        _make_tenant(db_manager)
        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/no-such-offering"
        )
        assert resp.status_code == 404

    def test_provision_unknown_tenant_404(self, auth_client_admin, db_manager, seed_app):
        self._seed_offering(db_manager, seed_app, usage={"markup_pct": 10})
        resp = auth_client_admin.post(
            f"/tenants/99999999-0000-7000-8000-000000000000/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 404

    def test_unprovision_clears_posture(self, auth_client_admin, db_manager, seed_app, monkeypatch):
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "byo_required")
        resp = auth_client_admin.delete(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-high-markup"
        )
        assert resp.status_code == 200
        assert resp.json()["posture_cleared"] is True
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"


class TestOfferingAccessPrice:
    """The ACCESS half of an offering (access_price_usd + access_cadence)."""

    def test_access_fields_persist(self, db_manager, seed_app):
        result = db_manager.create_offering(
            seed_app.key,
            "paid-managed",
            "Paid managed",
            "platform_only",
            access_price_usd=49.00,
            access_cadence="monthly",
        )
        assert result is not None
        assert result["access_price_usd"] == 49.0
        assert result["access_cadence"] == "monthly"

    def test_access_cadence_defaults_monthly(self, db_manager, seed_app):
        result = db_manager.create_offering(
            seed_app.key, "paid-default", "Paid", "byo_required", access_price_usd=10
        )
        assert result is not None and result["access_cadence"] == "monthly"

    def test_free_offering_has_null_price(self, db_manager, seed_app):
        result = db_manager.create_offering(seed_app.key, "free", "Free", "byo_required")
        assert result is not None and result["access_price_usd"] is None

    def test_invalid_access_cadence_raises(self, db_manager, seed_app):
        with pytest.raises(ValueError, match="invalid access_cadence"):
            db_manager.create_offering(
                seed_app.key, "x", "X", "byo_required", access_price_usd=1, access_cadence="weekly"
            )

    def test_post_creates_priced_offering(self, auth_client_admin, seed_app):
        resp = auth_client_admin.post(
            "/offerings",
            json={
                "app_key": seed_app.key,
                "code": "paid-managed",
                "name": "Paid managed",
                "byo_mode": "platform_only",
                "access_price_usd": 49,
                "access_cadence": "one_time",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_price_usd"] == 49.0
        assert body["access_cadence"] == "one_time"


class TestProvisionAccessChargeHelper:
    """The S2S billing access-charge write (`_provision_access_charge`)."""

    async def test_unset_url_returns_none(self, monkeypatch):
        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "")
        assert await main._provision_access_charge("t", "w2v-image", 49.0, "monthly") is None

    async def test_posts_correct_body_on_success(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        capture: dict = {}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(201), capture))

        status_code = await main._provision_access_charge("tid-1", "w2v-image", 49.0, "monthly")
        assert status_code == 201
        assert capture["url"].endswith("/api/config/access-charges")
        assert capture["json"] == {
            "tenant_id": "tid-1",
            "service_key": "w2v-image",  # pragma: allowlist secret -- product slug, not a credential
            "amount_usd": 49.0,
            "cadence": "monthly",
        }
        assert capture["headers"]["X-Tenant-Id"] == "tid-1"
        assert capture["headers"]["Authorization"] == "Bearer svc-token"
        assert_satisfies("AccessChargeCreate", capture["json"])

    async def test_non_2xx_returns_the_status(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(403, "forbidden"), {})
        )
        assert await main._provision_access_charge("t", "w2v-image", 5.0, "monthly") == 403


class TestProvisionAccessChargeRetryPolicy:
    """The same retry policy on the ACCESS half — a distinct code path.

    Mirrored deliberately: the two helpers were copy-pasted from each other, so a
    fix applied to one and not the other is the likeliest regression shape here.
    """

    def _install(self, monkeypatch, responses) -> tuple[list, list]:
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        calls: list = []
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _SequenceClient(responses, calls))
        return calls, _no_sleep(monkeypatch)

    async def test_403_is_not_retried(self, monkeypatch):
        import main

        calls, slept = self._install(monkeypatch, [_FakeResp(403, "forbidden")])
        assert await main._provision_access_charge("t", "w2v-image", 5.0, "monthly") == 403
        assert len(calls) == 1, f"a 403 must not be retried, got {len(calls)} attempts"
        assert slept == []

    async def test_503_then_201_succeeds_on_the_second_attempt(self, monkeypatch):
        import main

        calls, slept = self._install(
            monkeypatch, [_FakeResp(503, "rolling out"), _FakeResp(201, "")]
        )
        assert await main._provision_access_charge("t", "w2v-image", 5.0, "monthly") == 201
        assert len(calls) == 2
        assert slept == [1]

    async def test_transport_error_is_still_retried(self, monkeypatch):
        import httpx

        import main

        calls, slept = self._install(monkeypatch, [httpx.ConnectError("boom")])
        assert await main._provision_access_charge("t", "w2v-image", 5.0, "monthly") is None
        assert len(calls) == 2
        assert slept == [1]


class TestProvisionOfferingAccessCharge:
    """Provisioning a PRICED offering also writes the tenant access charge."""

    def _seed_priced(self, db_manager, seed_app, *, price=49.0, cadence="monthly"):
        return db_manager.create_offering(
            seed_app.key,
            "paid-managed",
            "Paid managed",
            "byo_required",
            usage={"markup_pct": 20},
            access_price_usd=price,
            access_cadence=cadence,
        )

    def test_provision_writes_access_charge(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_priced(db_manager, seed_app)

        async def _ok_markup(*a, **k):
            return 201

        captured: dict = {}

        async def _ok_access(tenant_id, service_key, amount_usd, cadence, included_cost_usd=None):
            captured.update(
                tenant_id=tenant_id,
                service_key=service_key,
                amount_usd=amount_usd,
                cadence=cadence,
                included_cost_usd=included_cost_usd,
            )
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _ok_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_charge_provisioned"] is True
        assert body["markup_rule_provisioned"] is True
        assert captured["service_key"] == seed_app.key
        assert captured["amount_usd"] == 49.0
        assert captured["cadence"] == "monthly"
        # No allowance on this offering → included_cost_usd forwarded as None.
        assert captured["included_cost_usd"] is None

    def test_free_offering_skips_access_charge(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key, "paid-managed", "Free", "byo_required", usage={"markup_pct": 20}
        )

        async def _ok_markup(*a, **k):
            return 201

        called = {"n": 0}

        async def _access(*a, **k):
            called["n"] += 1
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        )
        assert resp.status_code == 200
        assert resp.json()["access_charge_provisioned"] is False
        assert called["n"] == 0  # no price → access S2S not called

    def test_access_failure_rolls_back_posture(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        self._seed_priced(db_manager, seed_app)

        async def _ok_markup(*a, **k):
            return 201

        async def _fail_access(*a, **k):
            return 403

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _fail_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-managed"
        )
        assert resp.status_code == 502
        # Posture rolled back → reverts to tenant-wide.
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"


class TestOfferingIncludedAllowance:
    """The ALLOWANCE half of the ACCESS axis (included_allowance_usd)."""

    def test_allowance_persists(self, db_manager, seed_app):
        result = db_manager.create_offering(
            seed_app.key,
            "paid-with-credit",
            "Paid + credit",
            "byo_required",
            access_price_usd=49.0,
            included_allowance_usd=10.0,
        )
        assert result is not None
        assert result["included_allowance_usd"] == 10.0

    def test_allowance_defaults_null(self, db_manager, seed_app):
        result = db_manager.create_offering(seed_app.key, "free", "Free", "byo_required")
        assert result is not None and result["included_allowance_usd"] is None

    def test_post_creates_offering_with_allowance(self, auth_client_admin, seed_app):
        resp = auth_client_admin.post(
            "/offerings",
            json={
                "app_key": seed_app.key,
                "code": "paid-with-credit",
                "name": "Paid + credit",
                "byo_mode": "byo_required",
                "access_price_usd": 49,
                "included_allowance_usd": 10,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["included_allowance_usd"] == 10.0

    async def test_helper_posts_included_cost_when_set(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        capture: dict = {}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(201), capture))

        status_code = await main._provision_access_charge(
            "tid-1", "w2v-image", 49.0, "monthly", included_cost_usd=10.0
        )
        assert status_code == 201
        assert capture["json"] == {
            "tenant_id": "tid-1",
            "service_key": "w2v-image",  # pragma: allowlist secret -- product slug, not a credential
            "amount_usd": 49.0,
            "cadence": "monthly",
            "included_cost_usd": 10.0,
        }
        assert_satisfies("AccessChargeCreate", capture["json"])

    async def test_helper_omits_included_cost_when_unset(self, monkeypatch):
        import httpx

        import main
        import service_auth

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.test")
        monkeypatch.setattr(service_auth, "get_service_token_provider", lambda: _FakeProvider())
        capture: dict = {}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResp(201), capture))

        await main._provision_access_charge("tid-1", "w2v-image", 49.0, "monthly")
        assert "included_cost_usd" not in capture["json"]

    def test_provision_forwards_allowance(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key,
            "paid-with-credit",
            "Paid + credit",
            "byo_required",
            usage={"markup_pct": 20},
            access_price_usd=49.0,
            included_allowance_usd=10.0,
        )

        async def _ok_markup(*a, **k):
            return 201

        captured: dict = {}

        async def _ok_access(tenant_id, service_key, amount_usd, cadence, included_cost_usd=None):
            captured.update(amount_usd=amount_usd, included_cost_usd=included_cost_usd)
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _ok_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/paid-with-credit"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_charge_provisioned"] is True
        assert captured["amount_usd"] == 49.0
        assert captured["included_cost_usd"] == 10.0

    def test_free_offering_with_allowance_still_provisions(
        self, auth_client_admin, db_manager, seed_app, monkeypatch
    ):
        # A free (unpriced) offering that bundles a usage credit must STILL write
        # the access charge — amount 0, included N.
        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.create_offering(
            seed_app.key,
            "free-with-credit",
            "Free + credit",
            "byo_required",
            usage={"markup_pct": 20},
            included_allowance_usd=15.0,
        )

        async def _ok_markup(*a, **k):
            return 201

        captured: dict = {}

        async def _ok_access(tenant_id, service_key, amount_usd, cadence, included_cost_usd=None):
            captured.update(amount_usd=amount_usd, included_cost_usd=included_cost_usd)
            return 201

        monkeypatch.setattr("main._provision_offering_markup", _ok_markup)
        monkeypatch.setattr("main._provision_access_charge", _ok_access)

        resp = auth_client_admin.post(
            f"/tenants/{SEED_TENANT}/offerings/{seed_app.key}/free-with-credit"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_charge_provisioned"] is True
        assert captured["amount_usd"] == 0
        assert captured["included_cost_usd"] == 15.0


class TestKnownUnfixedRetryLoops:
    """The retry policy's SCOPE, derived from the corpus rather than asserted.

    ⚠️ The corpus is EVERY shipped module, not ``main.py``. It was ``main.py`` alone
    until 2026-09-10, when the three teardown loops were extracted into
    ``services/metering_teardown.py`` and this census went blind to them. See
    ``_corpus`` for why the repair was to widen the scan and never to shrink
    ``expected``.

    ``main.py`` held five ``for attempt in range(2)`` billing loops when this class
    was written. As of 2026-08-17 **all five** consult ``_is_retryable_status``.

    This class used to record the other shape: two fixed (the offering markup and
    access-charge writes) and three deliberately scoped out
    (``_fetch_license_sync_budget_ids``, ``_push_token_budget``,
    ``_push_quota_config``), with the note *"Promote them (and delete this test)
    in a PR that can prove the budget/quota path."* That PR is the one that
    changed these assertions: it drives a 403 on the budget push and a 422 on the
    quota push through the real reconcile route and counts the POSTs, with a 503
    control proving the retry is NARROWED and not removed.

    The scoping was right at the time and this class is why the promotion could
    not happen silently — it went red the moment the third and fourth loops moved.

    Stating that as a comment would be worth nothing, so both sets are derived
    from the source. What that derivation ACTUALLY guarantees, stated precisely
    because an earlier version of this docstring overclaimed:

    * The SET of bounded-retry billing loops in the SHIPPED CORPUS is pinned by
      enclosing-function name. Adding another such loop — the realistic way this
      defect spreads, by copying one of these helpers — turns both checks below
      red, in whichever module the copy lands. Verified by mutation, most recently
      2026-09-10 against a copy planted in ``services/metering_teardown.py``.
    * Adopting the shared policy in one of the three, i.e. writing
      ``_is_retryable_status`` into its loop, moves it between the two sets and
      is caught.

    It does NOT detect a hand-rolled equivalent: the walk keys on the identifier
    ``_is_retryable_status``, so a fix spelled ``if resp.status_code not in
    _RETRYABLE_STATUSES: return False`` inside ``_push_token_budget`` changes
    behaviour while leaving this green. That was measured, not assumed.
    Closing it would mean recognising retry semantics rather than a name, which
    is a static-analysis job out of proportion to a scope marker — and the
    direction that matters (the defect SPREADING) is covered.
    """

    #: Directories the census does not read. Copied from the sibling static gates
    #: (`test_no_naive_datetime_parsing._SKIP_DIRS`), `scripts/` DELIBERATELY included:
    #: `scripts/reap_expired_licences.py` now calls the teardown helpers, so a retry
    #: loop copied into a script is exactly the spread this census exists to catch.
    _SKIP_DIRS = frozenset(
        {"tests", "migrations", ".venv", ".worktrees", "certs", "docs", "__pycache__"}
    )

    #: Modules that MUST be in the corpus, or every assertion below is vacuous.
    #: `services/metering_teardown.py` is listed because it is where the three
    #: teardown loops LIVE as of the 2026-09-10 extraction — if a later move empties
    #: the walk, the floor says so instead of the census quietly reporting six loops.
    _CORPUS_FLOOR = ("main.py", "services/metering_teardown.py")

    @classmethod
    def _corpus(cls) -> list[Path]:
        """Every shipped module, DERIVED — not a hard-coded file list.

        This scan used to read ``main.py`` and nothing else. On 2026-09-10
        ``_teardown_token_budget``, ``_teardown_quota_config`` and
        ``_fetch_active_budget_ids_for_license`` were extracted out of ``main.py``
        into ``services/metering_teardown.py`` (licenses#195) and the census stopped
        seeing three of the nine loops it guards — it went red, which is the census
        working, but the tempting repair (shrinking ``expected`` to the six loops
        still in ``main.py``) would have turned it green while permanently
        UN-guarding the three that moved. ⛔ That is the wrong repair: the whole
        point of this class is to notice a retry helper being copied without
        ``_is_retryable_status``, and it cannot notice that in a file it does not
        read. So the corpus is derived from the tree instead of named, and the NEXT
        extraction is covered without anybody remembering to widen it.
        """
        return sorted(
            p
            for p in REPO_ROOT.rglob("*.py")
            if not any(part in cls._SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
        )

    @classmethod
    def _retry_loops(cls) -> dict[str, bool]:
        """{enclosing function name: does its retry loop consult the policy helper}.

        Keyed by function NAME across the whole corpus, so two modules defining the
        same helper name collapse into one entry — combined with AND, so a
        non-compliant copy anywhere in the tree still reds the second assertion.
        """
        found: dict[str, bool] = {}
        for path in cls._corpus():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                for loop in ast.walk(func):
                    if not isinstance(loop, ast.For):
                        continue
                    if not (
                        isinstance(loop.target, ast.Name)
                        and loop.target.id == "attempt"
                        and isinstance(loop.iter, ast.Call)
                        and isinstance(loop.iter.func, ast.Name)
                        and loop.iter.func.id == "range"
                    ):
                        continue
                    names = {node.id for node in ast.walk(loop) if isinstance(node, ast.Name)}
                    # BOTH policies, not just the status one. The status-only version
                    # of this census was GREEN while `_provision_offering_markup` and
                    # `_provision_access_charge` still retried a DETERMINISTIC
                    # transport error unconditionally — it recognised one identifier
                    # and the other arm does not contain it. Found by an adversarial
                    # census after #144 shipped.
                    compliant = (
                        "_is_retryable_status" in names and "_is_retryable_transport_error" in names
                    )
                    # AND, never assign. A plain assignment recorded only the LAST
                    # loop walked, so a newly added non-compliant loop inside an
                    # ALREADY-compliant helper was silently overwritten and reported
                    # as fixed — and extending one of these helpers is at least as
                    # likely as copying it into a new one. Found by an adversarial
                    # reviewer.
                    found[func.name] = found.get(func.name, True) and compliant
        return found

    def test_the_corpus_reaches_the_modules_this_census_is_about(self):
        """Anti-vacuity: a walk that reads nothing satisfies nothing.

        Without this, a broken derivation (a bad skip entry, a moved REPO_ROOT)
        would report ZERO loops — and a zero census reads exactly like a clean one.
        """
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in self._corpus()}
        assert len(rels) >= 10, f"only {len(rels)} shipped modules found; the walk is broken"
        missing = [m for m in self._CORPUS_FLOOR if m not in rels]
        assert not missing, f"corpus no longer reaches {missing}; this census is vacuous"

    def test_every_bounded_retry_billing_loop_honours_the_retry_policy(self):
        loops = self._retry_loops()
        expected = {
            "_fetch_license_sync_budget_ids",
            # The quota half of the same read-before-write pair, added 2026-08-24.
            # It is a copy of the budget read's retry shape, which is exactly the
            # spread this census watches for — so it is listed only because both
            # policy helpers are wired into it.
            "_fetch_quota_config_monthly",
            "_push_token_budget",
            "_push_quota_config",
            "_provision_offering_markup",
            "_provision_access_charge",
            # The teardown half of the quota-config pair, added 2026-09-06 with
            # the revocation fast path. It is the FIRST outbound DELETE to
            # billing in this repo and its loop was written from
            # `_provision_offering_markup`'s shape — precisely the copying this
            # census exists to catch — so it is listed only because both policy
            # helpers are wired into it. A 403 here is a verdict about the
            # licences SA's realm roles, and a 404 means billing has no such
            # route (its `delete_quota_config` answers 200 with `deleted="0"`
            # when the row is already absent); neither is worth a second send.
            "_teardown_quota_config",
            # The token-BUDGET half of the same revocation fast path, added
            # 2026-09-10. Listed for the same reason and on the same terms: both
            # policy helpers are wired into it, and its loop was written from
            # `_teardown_quota_config`'s shape — the exact copying this census
            # exists to catch.
            #
            # One difference from its sibling, and it is deliberate: here a 404
            # is treated as SUCCESS, not as residue. Billing's
            # `DELETE /api/budget/{budget_id}` is itself a soft delete and
            # answers 404 for a row that is already inactive, so a 404 means the
            # teardown's goal is already met. On the quota route a 404 means
            # billing has no such route at all, which IS residue.
            "_teardown_token_budget",
            # The read that feeds the teardown: which budget ROWS must die with
            # this licence. A sibling of `_fetch_license_sync_budget_ids` — same
            # endpoint and envelope, opposite question — so it carries the same
            # tri-state discipline, and `None` means UNKNOWN and must never be
            # read as "nothing to tear down".
            "_fetch_active_budget_ids_for_license",
        }
        assert set(loops) == expected, (
            f"the set of bounded-retry billing loops in the shipped corpus changed: "
            f"{sorted(loops)}. A further such loop is the realistic way this defect "
            "spreads — by copying one of these helpers — so add it to `expected` only "
            "after wiring it to `_is_retryable_status`. ⛔ If a loop went MISSING because "
            "it moved to another module, widen `_SKIP_DIRS`/`_corpus` — never shrink "
            "`expected`, which would un-guard the loop for good."
        )

        unfixed = {name for name, ok in loops.items() if not ok}
        assert not unfixed, (
            f"these billing retry loops re-send a VERDICT: {sorted(unfixed)}. "
            "A 403/422 answers identically a second later; only "
            "{408,425,429,500,502,503,504} are worth retrying."
        )


class TestRestorePostureFailureIsLoud:
    """``_restore_tenant_app_posture`` must never fail quietly.

    Its caller's very next statement raises a 502 telling the operator the
    posture "was rolled back". If the restore did nothing, or blew up, and this
    helper absorbed it, that 502 becomes the misinformation — and nothing else in
    the system would ever say otherwise. Measured gap before these: wrapping the
    helper body in ``except Exception: pass`` survived the entire suite.
    """

    def test_a_restore_that_finds_no_tenant_logs_an_error(self, db_manager, monkeypatch, caplog):
        import logging

        import main

        monkeypatch.setattr(main, "db_manager", db_manager)
        monkeypatch.setattr(db_manager, "set_tenant_app_byo_mode", lambda *a, **k: None)

        with caplog.at_level(logging.ERROR, logger="main"):
            main._restore_tenant_app_posture("gone-tenant", "some-app", "platform_only")

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "a rollback that restored nothing was silent"
        assert "rollback FAILED" in errors[0].getMessage()
        assert "platform_only" in errors[0].getMessage()

    def test_a_clear_that_removes_nothing_logs_an_error(self, db_manager, monkeypatch, caplog):
        import logging

        import main

        monkeypatch.setattr(main, "db_manager", db_manager)
        monkeypatch.setattr(db_manager, "clear_tenant_app_byo_mode", lambda *a, **k: False)

        with caplog.at_level(logging.ERROR, logger="main"):
            main._restore_tenant_app_posture("some-tenant", "some-app", None)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "a rollback that cleared nothing was silent"
        assert "CLEARED NOTHING" in errors[0].getMessage()

    def test_a_successful_restore_logs_no_error(self, db_manager, seed_app, monkeypatch, caplog):
        """The other half: the normal path must stay quiet.

        Without this, an implementation that logs unconditionally would satisfy
        both checks above while making every rollback look like a failure.
        """
        import logging

        import main

        _make_tenant(db_manager, byo_mode="local_allowed")
        db_manager.set_tenant_app_byo_mode(SEED_TENANT, seed_app.key, "byo_required")
        monkeypatch.setattr(main, "db_manager", db_manager)

        with caplog.at_level(logging.ERROR, logger="main"):
            main._restore_tenant_app_posture(SEED_TENANT, seed_app.key, "platform_only")
            main._restore_tenant_app_posture(SEED_TENANT, seed_app.key, None)

        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
        assert db_manager._lookup_byo_mode(SEED_TENANT, app_key=seed_app.key) == "local_allowed"

    def test_a_raising_restore_propagates(self, db_manager, monkeypatch):
        """A failed rollback must NOT be downgraded into the caller's 502.

        "The rollback itself failed" is strictly worse news than "billing refused
        the write", so it gets the louder answer (a 500), not the quieter one.
        """
        import main

        def _boom(*a, **k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(main, "db_manager", db_manager)
        monkeypatch.setattr(db_manager, "set_tenant_app_byo_mode", _boom)

        with pytest.raises(RuntimeError, match="connection reset"):
            main._restore_tenant_app_posture("t", "a", "platform_only")


class TestOfferingMoneyIsNonNegative:
    """An offering's absolute surcharges must be bounded ``ge=0`` like its siblings.

    ``markup_pct``, ``access_price_usd`` and ``included_allowance_usd`` all carry
    ``ge=0``; the five ``surcharge_*`` fields did not. That asymmetry mattered
    once billing_service #78 bounded the same five fields on ``MarkupRuleCreate``
    (because ``services.markup.compute_billed_usd`` *adds* them, so a negative is
    a silent discount): an offering carrying a negative surcharge saved happily
    here, and only failed later — as an opaque ``502 Billing rejected the
    markup-rule write (HTTP 422); provisioning rolled back`` — when a super-admin
    pressed "Provision" in the fr UI. The money is validated in exactly one place
    downstream, so the error surfaced two services away from the field that
    caused it.

    ``_OFFERING_USAGE_FIELDS`` is field-for-field identical to billing's
    ``MARKUP_ECONOMIC_FIELDS``, and ``provision_tenant_offering`` builds the
    markup-rule body straight from it — so these bounds must agree with billing's
    or the two disagree about the same five numbers.
    """

    NEGATIVE_MONEY = [
        "surcharge_input_per_1m_usd",
        "surcharge_output_per_1m_usd",
        "surcharge_cache_creation_per_1m_usd",
        "surcharge_cache_read_per_1m_usd",
        "surcharge_per_request_usd",
    ]

    def _body(self, app_key: str, **extra):
        return {
            "app_key": app_key,
            "code": "money-bounds",
            "name": "Money bounds",
            "byo_mode": "local_allowed",
            **extra,
        }

    @pytest.mark.parametrize("field", NEGATIVE_MONEY)
    def test_a_negative_surcharge_is_refused(self, auth_client_admin, seed_app, field):
        resp = auth_client_admin.post("/offerings", json=self._body(seed_app.key, **{field: -0.01}))
        assert resp.status_code == 422, resp.text

    def test_control_negative_markup_pct_was_already_refused(self, auth_client_admin, seed_app):
        """The in-harness control: this bound already existed, so a harness that
        cannot tell 422 from 200 would fail HERE too, not only on the new fields."""
        resp = auth_client_admin.post("/offerings", json=self._body(seed_app.key, markup_pct=-1))
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize("field", NEGATIVE_MONEY)
    def test_zero_is_still_accepted(self, auth_client_admin, seed_app, field):
        """GREEN half. 0 is an explicit "charge nothing on this dimension", not an
        absent field — mirrors billing's own zero-is-valid rule. A ``gt=0`` bound
        would pass every negative test above and still be wrong."""
        resp = auth_client_admin.post("/offerings", json=self._body(seed_app.key, **{field: 0}))
        assert resp.status_code == 200, resp.text
        assert resp.json()[field] == 0

    def test_a_positive_surcharge_is_still_accepted(self, auth_client_admin, seed_app):
        resp = auth_client_admin.post(
            "/offerings", json=self._body(seed_app.key, surcharge_input_per_1m_usd=1.25)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["surcharge_input_per_1m_usd"] == 1.25


class TestTheRetryPredicateItself:
    """`_is_retryable_status` is load-bearing at FIVE call sites and had none of its own.

    Found by an adversarial reviewer: with only the per-helper tests, the mutant
    ``def _is_retryable_status(code): return code >= 500`` SURVIVES everything.
    The AST census stays green (it keys on the identifier, still present); the
    offering tests stay green (400/403/404/422 don't retry, 503 does); all four
    metering tests stay green (403/422 don't, 503 does).

    What it silently changes is 408/425/429 — and **429 is the status most likely
    to actually appear** when a 5000-licence sweep hammers billing, which is
    exactly the case the retry exists for.

    Membership is asserted per code rather than by comparing the whole frozenset,
    so the failure message names the offending status instead of dumping two sets.
    """

    import pytest as _pytest

    @_pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
    def test_retryable_statuses(self, code: int):
        import main

        assert main._is_retryable_status(code), (
            f"{code} is a transient condition — 'ask again' — and must be retried"
        )

    @_pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 410, 422, 501, 505])
    def test_verdict_statuses(self, code: int):
        import main

        assert not main._is_retryable_status(code), (
            f"{code} is a VERDICT — re-sending the identical request cannot change it"
        )

    def test_a_2xx_is_not_routed_through_this_predicate(self):
        """Guard against a future caller using it as a success test.

        Every call site checks ``< 300`` FIRST and only consults this predicate on
        a non-2xx, so 2xx membership is undefined by design. Pinning it as False
        stops someone reading the helper as "should I keep going".
        """
        import main

        assert not main._is_retryable_status(200)
        assert not main._is_retryable_status(201)


class TestATransportVerdictIsNotRetried:
    """`except httpx.HTTPError` spans two different things, and only one is a blip.

    `httpx.UnsupportedProtocol` is raised BEFORE any I/O when the URL has no
    `http(s)://` scheme. `METERING_SERVICE_URL` defaults to `""`, and unlike the
    two offering helpers the three metering helpers do not guard on it — so a
    scheme-less or unset URL made every attempt raise identically, one wasted
    second apart.

    At the reconcile route's `le=5000` ceiling that is ~83 minutes of
    `asyncio.sleep` inside a single HTTP request, for an answer fixed at the first
    attempt: verbatim the harm the status-side narrowing was written to remove,
    reached through the exception arm instead of the status arm.

    The structural census in `TestKnownUnfixedRetryLoops` CANNOT see this — it
    inspects the `for attempt` body, and the identifier is not in the `except`
    arm. That is why this is a behavioural test.
    """

    @staticmethod
    def _install(monkeypatch, exc: Exception):
        """These helpers take the client as their FIRST ARGUMENT.

        Patching `httpx.AsyncClient` does nothing here — the first draft did that,
        passed `None` as the client, and got an `AttributeError` instead of a
        measurement. An AttributeError is never a RED proof.
        """
        calls: list = []

        class _Boom:
            async def post(self, *a, **k):
                calls.append(("post", a, k))
                raise exc

            async def get(self, *a, **k):
                calls.append(("get", a, k))
                raise exc

        return _Boom(), calls, _no_sleep(monkeypatch)

    async def test_an_unsupported_protocol_is_attempted_once(self, monkeypatch):
        """RED before the fix: 2 attempts and a 1s sleep, per licence."""
        import httpx

        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "metering-service:8000")
        client, calls, slept = self._install(
            monkeypatch, httpx.UnsupportedProtocol("Request URL is missing an 'http://'")
        )

        ok = await main._push_quota_config(
            client, license_id="lic", tenant_id="t", quota_monthly=1, headers={}
        )

        assert ok is False
        assert len(calls) == 1, f"a scheme-less URL cannot succeed on retry, got {len(calls)}"
        assert slept == [], f"slept {slept} on a deterministic transport error"

    async def test_a_connect_error_is_still_retried(self, monkeypatch):
        """The control: a genuine network blip must keep its retry.

        Without this, deleting the transport retry outright would pass the test
        above — the same narrowing-vs-removal distinction the 503 status control
        exists for.
        """
        import httpx

        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://metering:8000")
        client, calls, slept = self._install(monkeypatch, httpx.ConnectError("connection refused"))

        ok = await main._push_quota_config(
            client, license_id="lic", tenant_id="t", quota_monthly=1, headers={}
        )

        assert ok is False
        assert len(calls) == 2, f"a connect error is transient and must be retried: {len(calls)}"
        assert slept == [1], f"a retryable transport error must back off once, got {slept}"


class TestOfferingHelpersDoNotRetryATransportVerdict:
    """The offering half of the transport gate, behaviourally.

    `TestKnownUnfixedRetryLoops` is a STRUCTURAL census — it recognises an
    identifier inside the `for attempt` body. It was green for these two helpers
    while both still retried `UnsupportedProtocol` unconditionally, because the
    identifier it looked for (`_is_retryable_status`) is in the STATUS arm and the
    fault is in the `except` arm.

    Why it matters here specifically: `provision_tenant_offering` calls BOTH
    helpers in sequence, so one `POST /tenants/{id}/offerings/{app}/{code}` spent
    2 seconds of `asyncio.sleep` on the event loop — on a pod that also serves
    `/license/introspect` — before returning a 502 whose answer was fixed at the
    first attempt.

    The empty-string guard in each helper does NOT cover this: it catches
    METERING_SERVICE_URL being UNSET, not being SET WRONG. A scheme-less value
    like `billing.licenses.svc:8080` is truthy.
    """

    @staticmethod
    def _client(monkeypatch, exc: Exception):
        calls: list = []

        class _Boom:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                calls.append(a[0] if a else None)
                raise exc

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Boom())
        return calls, _no_sleep(monkeypatch)

    async def test_markup_rule_stops_on_a_scheme_less_url(self, monkeypatch):
        import httpx

        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "billing.licenses.svc:8080")
        calls, slept = self._client(
            monkeypatch, httpx.UnsupportedProtocol("Request URL is missing an 'http://'")
        )
        await main._provision_offering_markup("t", "some-app", {"markup_pct": 5.0})

        assert len(calls) == 1, f"a scheme-less URL cannot succeed on retry, got {len(calls)}"
        assert slept == [], f"slept {slept} on a deterministic transport verdict"

    async def test_access_charge_stops_on_a_scheme_less_url(self, monkeypatch):
        import httpx

        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "billing.licenses.svc:8080")
        calls, slept = self._client(monkeypatch, httpx.UnsupportedProtocol("no scheme"))
        await main._provision_access_charge("t", "some-app", amount_usd=1.0, cadence="monthly")

        assert len(calls) == 1, f"expected one attempt, got {len(calls)}"
        assert slept == [], f"slept {slept} on a deterministic transport verdict"

    async def test_a_connect_error_is_still_retried(self, monkeypatch):
        """The control — this must remain a NARROWING, not a removal."""
        import httpx

        import main

        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing:8080")
        calls, slept = self._client(monkeypatch, httpx.ConnectError("refused"))
        await main._provision_offering_markup("t", "some-app", {"markup_pct": 5.0})

        assert len(calls) == 2, f"a connect error is transient and must retry: {len(calls)}"
        assert slept == [1], f"a retryable transport error must back off once, got {slept}"


class TestThePerAppReadThatWasMissing:
    """``GET /tenants/{n}/apps/{app_key}/byo-mode`` — the read that completes the set.

    PUT and DELETE have existed on this exact path since posture rows were
    introduced. The matching READ did not, so answering "what is this ONE
    product's override" meant fetching every override the tenant has and
    filtering client-side: an unbounded response to a bounded question.
    """

    def test_returns_the_override_row_for_one_app(self, auth_client_admin, db_manager, seed_app):
        _make_tenant(db_manager)
        auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "platform_only"},
        )

        resp = auth_client_admin.get(f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["app_key"] == seed_app.key
        assert body["byo_mode"] == "platform_only"

    def test_no_override_is_404_not_a_fabricated_default(
        self, auth_client_admin, db_manager, seed_app
    ):
        # The dangerous alternative is answering 200 with the tenant-wide value.
        # That would make "no override" indistinguishable from "an override that
        # happens to match", and a caller treating this as the EFFECTIVE posture
        # would report byo_required as local_allowed. 404 forces the caller to
        # decide, and the tenant-wide fallback lives on the other route.
        _make_tenant(db_manager, byo_mode="byo_required")

        resp = auth_client_admin.get(f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode")

        assert resp.status_code == 404, resp.text

    def test_unknown_tenant_is_404(self, auth_client_admin, seed_app):
        resp = auth_client_admin.get(
            f"/tenants/01000000-0000-7000-8000-0000000000ff/apps/{seed_app.key}/byo-mode"
        )
        assert resp.status_code == 404

    def test_an_admin_is_served_which_is_what_the_exemption_would_break(
        self, auth_client_admin, db_manager, seed_app
    ):
        """⛔ THE PIN. Keep this route OUT of ``_BYO_MODE_OPEN_ROUTES``.

        That pattern used to carry a second alternation matching this exact path
        shape. It was dormant — only PUT and DELETE existed here, so for GET it
        matched requests that then 404'd — and adding this GET activates it.

        ⚠️ What it activates is NOT an auth bypass, which is what it looks like.
        MEASURED by restoring the alternation: an authenticated **license-admin**
        gets 401. The exemption ends in ``return await call_next(request)``
        WITHOUT authenticating, so ``request.state.user`` is never populated —
        and this route, unlike the tokenless tenant-wide one, carries
        ``Depends(require_any_role(...))``, which then refuses everyone. The
        route would be unusable by anybody, admins included.

        So the assertion is that an ADMIN IS SERVED. Asserting "anonymous gets
        401" would look like the security test and prove nothing: anonymous is
        refused either way, by the dependency, so that test passes just as
        happily with the exemption restored. Verified by mutation — this case
        goes red (401 == 200) when the alternation comes back; the anonymous one
        stays green.
        """
        _make_tenant(db_manager)
        auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "platform_only"},
        )

        resp = auth_client_admin.get(f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode")

        assert resp.status_code == 200, resp.text

    def test_an_anonymous_caller_is_still_refused(self, client, db_manager, seed_app):
        """The other half — the route is not readable without a token.

        Weak on its own (see above: it passes with the exemption restored too),
        but it is what says the route never becomes genuinely open if the
        dependency is ever dropped.
        """
        _make_tenant(db_manager)

        resp = client.get(f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode")

        assert resp.status_code == 401, resp.text

    def test_the_tenant_wide_lookup_is_still_tokenless(self, client, db_manager):
        """The negative control for the test above.

        Narrowing the regex had to remove the per-app shape WITHOUT disturbing
        the tenant-wide one. worker_management calls that one with no token and
        FAILS OPEN on any non-200, so a 401 there does not break visibly — it
        silently stops enforcing byo_required fleet-wide. Asserting only that the
        per-app shape is closed would not have noticed that.
        """
        _make_tenant(db_manager)

        assert client.get(f"/tenants/{SEED_TENANT}/byo-mode").status_code != 401


class TestNullProvenanceSaysWhatItMeans:
    """``offering_code: null`` means UNKNOWN, and the payload now says so."""

    def test_a_directly_set_posture_reports_unknown_provenance(
        self, auth_client_admin, db_manager, seed_app
    ):
        # Set directly via the per-app PUT: no offering produced it, so the code
        # is null. Every docstring since migration 025 has warned that null means
        # "provenance unknown" and NOT "no offering" — but a docstring cannot
        # reach the consumer, and over the wire `null` in a field called
        # `offering_code` reads as "there is no offering". The explicit field is
        # the warning, delivered where the caller actually looks.
        _make_tenant(db_manager)
        auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "platform_only"},
        )

        body = auth_client_admin.get(f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode").json()

        assert body["offering_code"] is None
        assert body["offering_provenance"] == "unknown"

    def test_the_list_view_carries_it_too(self, auth_client_admin, db_manager, seed_app):
        # The aggregate is where an operator actually reads these, so the field
        # has to be on both or the distinction is only half-delivered.
        _make_tenant(db_manager)
        auth_client_admin.put(
            f"/tenants/{SEED_TENANT}/apps/{seed_app.key}/byo-mode",
            json={"byo_mode": "platform_only"},
        )

        rows = auth_client_admin.get(f"/tenants/{SEED_TENANT}/offerings").json()

        assert len(rows) == 1
        assert rows[0]["offering_provenance"] == "unknown"

    def test_it_is_derived_from_the_code_and_cannot_drift(self):
        # A stored duplicate of "is offering_code set" would be one more thing to
        # keep in sync, and it would be wrong exactly when it mattered.
        from main import TenantOfferingRecord

        recorded = TenantOfferingRecord(
            app_key="a", app_name="A", byo_mode="byo_required", offering_code="free-high-markup"
        )
        unknown = TenantOfferingRecord(app_key="a", app_name="A", byo_mode="byo_required")

        assert recorded.offering_provenance == "recorded"
        assert unknown.offering_provenance == "unknown"
