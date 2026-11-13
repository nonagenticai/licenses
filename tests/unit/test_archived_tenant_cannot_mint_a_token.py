"""Archiving a tenant must also stop it being issued NEW credentials.

⛔ THE GAP. Archiving already refused in two places — `/license/introspect` answers
``active: false, reason: "tenant_archived"``
(``tests/unit/test_archived_tenant_gates_spend.py``) and ``POST /licenses`` will not
mint a LICENCE for an archived tenant
(``tests/unit/test_mint_refuses_an_archived_tenant.py``). Neither covered
``POST /licenses/{license_id}/tokens``. An operator who retired a tenant could still
mint a fresh, ``expires_days=None`` (PERPETUAL) token against a licence that tenant
already held: a credential born AFTER the retirement, and invisible in
``list_tenants`` because archived rows are filtered out of the picker. Measured on
``origin/main``, every assertion in ``TestTokenMintRefusesAnArchivedTenant`` returned
**201**.

⚠️ WHAT THIS IS WORTH, STATED HONESTLY — because overstating it is how a gate gets
trusted for something it does not do. The minted token would be refused at introspect
anyway while ``archived_at`` is set, so this is NOT the difference between spending
and not spending today. What it prevents is a credential whose existence OUTLIVES the
decision to retire the tenant: un-archive later, for any reason, and every token
issued in the interval silently becomes live. The gate keeps the credential set a
restored tenant wakes up with equal to the set it had when it was retired.
``TestUnArchivingIsStillATrueRestore`` is what pins that framing.

⛔ AND WHAT IT STILL CANNOT STOP. All three gates share one precondition: the caller
has to ASK this service something. A cost event posted against a bare ``tenant_id``
with ``license_id = NULL`` never does — ``rmt-e2e``, archived 2026-09-03, accrued 461
such events / $1.60 in the following nine days, from ``inference-router`` and
``mlx-ingestion-backend``. ⛔ NOTHING IN THIS REPO CAN REACH THAT TRAFFIC, and no test
here should be read as claiming otherwise. ``TestWhatArchivingStillCannotStop``
exists so that the limit is executable rather than a paragraph someone stops reading.

⛔ REFUSAL, NOT REVOCATION — the decision ``set_tenant_archived`` documents, and this
change does not reopen it. Nothing here revokes an existing token, writes
``revoked_at``, or touches a row. Revoking would make archiving destructive and
therefore not reversible, buying an off-switch by removing the restore.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from models import Token

ARCHIVED_TENANT = "01000000-0000-7000-8000-0000000000c1"

# ── The discriminator for "a route that INGESTS a cost or usage event" ───────────
#
# ⚠️ THIS USED TO BE `"cost" in path`, AND THAT PREDICATE WAS TOO BROAD. It matched
# `PATCH /tenants/{name_or_id}/cost-centre`, which is an accounting LABEL on the
# tenant row (a closed vocabulary, `ck_tenants_cost_centre`, migration 034) — it
# records no spend, creates no event and moves no money. The premise the test is
# defending is unchanged and still true; only the way it recognised its subject was
# wrong.
#
# A cost/usage event has two properties a substring cannot see, and this asks for
# BOTH:
#
#   1. IT IS WRITTEN AS A NEW RECORD — so the method must be one that creates
#      (`POST`/`PUT`). A `PATCH` sets a field on something that already exists,
#      which is what a cost-centre label is. A `GET` reads; a reporting endpoint
#      over someone else's cost data would not give the archived-tenant gates a
#      population to cover, because nothing is being accrued.
#   2. THE RECORD IS THE COST/USAGE ITSELF — so the resource being written, i.e.
#      the LAST path segment, has to name that record rather than name a property
#      of another entity. `cost-centre` is a property of a tenant; `cost-events`
#      is the thing itself.
#
# Requiring both is what makes the test still fail for a real ingestion route while
# passing for the label endpoint. `TestTheCostRouteDiscriminatorActuallyDiscriminates`
# proves that on synthetic shapes, so the narrowing cannot quietly become a
# predicate that matches nothing.
_INGESTION_METHODS = frozenset({"POST", "PUT"})

_COST_RECORD_SEGMENTS = frozenset(
    {
        "cost",
        "costs",
        "cost-event",
        "cost-events",
        "usage",
        "usages",
        "usage-event",
        "usage-events",
        "meter",
        "metering",
        "spend",
        "consumption",
    }
)


def _is_cost_or_usage_ingestion(method: str, path: str) -> bool:
    """True iff (method, path) is a route that would RECORD a cost or usage event."""
    if method.upper() not in _INGESTION_METHODS:
        return False
    segments = [segment for segment in path.lower().split("/") if segment]
    if not segments:
        return False
    return segments[-1] in _COST_RECORD_SEGMENTS


def _cost_ingestion_routes() -> list[str]:
    """Every `METHOD /path` on this app that the discriminator calls an ingestion."""
    found = []
    for route in main.app.routes:
        # `getattr` on BOTH sides: `app.routes` is typed `list[BaseRoute]`, which
        # has neither `.path` nor `.methods` — only the concrete subclasses do, and
        # `ty` rejects a bare attribute access here.
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or ():
            if _is_cost_or_usage_ingestion(method, path):
                found.append(f"{method} {path}")
    return sorted(found)


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


@pytest.fixture
def licence_on_a_tenant_about_to_be_archived(main_db, seed_app, seed_plan, seed_subject):
    """An ACTIVE licence, minted while the tenant was live, then archived.

    The ordering is the point: this is the only interesting case. A licence minted
    AFTER the archive cannot exist (``POST /licenses`` refuses it), so the tokens a
    retired tenant can still ask for are exactly the ones hanging off licences it
    already had.
    """
    main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-token-corp", owner_key="platform")
    licence = main_db.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=ARCHIVED_TENANT,
        status="active",
    )
    return licence


def _mint_token(client: TestClient, licence_id, tenant_id: str, **params):
    return client.post(
        f"/licenses/{licence_id}/tokens",
        params=params,
        headers={"X-Tenant-Id": tenant_id},
    )


def _tokens_for(db_manager, licence_id) -> list[Token]:
    session = db_manager.get_session()
    try:
        return session.query(Token).filter(Token.license_id == UUID(str(licence_id))).all()
    finally:
        session.close()


class TestTokenMintRefusesAnArchivedTenant:
    """The defect. On ``origin/main`` every assertion here measured a 201."""

    def test_an_archived_tenant_cannot_be_issued_a_new_token(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        licence = licence_on_a_tenant_about_to_be_archived
        main_db.set_tenant_archived("retired-token-corp", True)

        response = _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)

        assert response.status_code == 400, (
            f"a retired tenant was issued a brand-new credential; body={response.text}"
        )
        assert "archived" in response.json()["detail"].lower(), response.text

    def test_the_refusal_writes_no_token_row(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        """⛔ A 400 that still persisted the row would be the WORST outcome: the
        caller is told no, and a live credential exists anyway — one nobody knows to
        go looking for, because the API said it was not created. The guard runs
        BEFORE ``create_token``, and this is what proves it."""
        licence = licence_on_a_tenant_about_to_be_archived
        main_db.set_tenant_archived("retired-token-corp", True)

        _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)

        assert _tokens_for(main_db, licence.id) == [], (
            "the mint was refused but a token row was written anyway"
        )

    def test_a_perpetual_token_is_refused_too(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        """The worst shape specifically. ``expires_days`` omitted means ``expires_at
        = NULL``, which ``Token.is_valid()`` reads as "never expires" — a credential
        with no natural end, minted against a tenant that has been retired."""
        licence = licence_on_a_tenant_about_to_be_archived
        main_db.set_tenant_archived("retired-token-corp", True)

        response = _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)
        assert response.status_code == 400, response.text
        assert _tokens_for(main_db, licence.id) == []


class TestTheLiveTenantIsUnaffected:
    """⛔ THE NEGATIVE CONTROL, and the one that decides whether the gate is a gate.

    Without it a guard that refused EVERY mint — or one keyed on the wrong column —
    would pass every assertion above. It is also the regression this change is most
    likely to cause: token minting is an ordinary provisioning operation on every
    live tenant in the estate.
    """

    def test_a_live_tenant_can_still_be_issued_a_token(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        licence = licence_on_a_tenant_about_to_be_archived  # registered, NOT archived

        response = _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)

        assert response.status_code == 201, (
            "token minting is broken for a LIVE tenant — the guard is keyed on "
            f"something other than archived_at; body={response.text}"
        )
        assert response.json()["token"], "no raw token was returned"
        assert len(_tokens_for(main_db, licence.id)) == 1

    def test_un_archiving_restores_the_ability_to_mint(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        """Refusal, not revocation: the gate is keyed on one nullable column and is
        reversible by definition. If this fails, archiving has become destructive."""
        licence = licence_on_a_tenant_about_to_be_archived

        main_db.set_tenant_archived("retired-token-corp", True)
        assert _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT).status_code == 400

        # Restored by id: since migration 053 a name resolves to LIVE tenants only.
        main_db.set_tenant_archived(ARCHIVED_TENANT, False)
        after = _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)
        assert after.status_code == 201, f"un-archiving did not restore minting; body={after.text}"


class TestUnArchivingIsStillATrueRestore:
    """⛔ ARCHIVING REVOKES NOTHING, AND THIS CHANGE DOES NOT ALTER THAT.

    The obvious "fix" for a tenant that keeps spending is to revoke its licences on
    archive. That was considered and refused: it makes archiving destructive, so
    un-archiving stops being a restore, and an operator retiring a tenant by mistake
    could not undo it. This class fails if someone later adds that revocation.
    """

    def test_an_existing_token_is_not_revoked_by_archiving(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        licence = licence_on_a_tenant_about_to_be_archived
        minted = _mint_token(auth_client_admin, licence.id, ARCHIVED_TENANT)
        assert minted.status_code == 201, minted.text

        main_db.set_tenant_archived("retired-token-corp", True)

        tokens = _tokens_for(main_db, licence.id)
        assert tokens, "archiving destroyed a token row"
        assert all(not t.revoked for t in tokens), (
            "archiving REVOKED an existing token. Archiving is a refusal, not a "
            "revocation — making it destructive removes the restore that is the "
            "entire reason it is not a delete."
        )

    def test_the_licence_row_itself_survives_archiving(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        licence = licence_on_a_tenant_about_to_be_archived
        main_db.set_tenant_archived("retired-token-corp", True)

        still_there = main_db.get_license(UUID(str(licence.id)))
        assert still_there is not None, "archiving destroyed a licence row"
        assert still_there.status == "active", (
            "archiving wrote a status transition onto the licence; it must leave "
            "stored state alone so un-archiving is a true restore"
        )


class TestWhatArchivingStillCannotStop:
    """⛔ THE LIMIT, MADE EXECUTABLE RATHER THAN LEFT IN PROSE.

    Every gate in this service shares one precondition: the caller has to ASK this
    service something. A writer that posts a cost event against a bare ``tenant_id``
    never does, and no gate added in this repo will change that — closing it means
    requiring a RESOLVED licence at the cost-event writer, which lives elsewhere.

    This test asserts the boundary, so a future reader cannot mistake a green suite
    here for "an archived tenant cannot spend".
    """

    def test_this_service_has_no_route_that_records_a_cost_event(self) -> None:
        """⚠️ THE PREDICATE WAS NARROWED; THE PREMISE WAS NOT.

        This asserted ``"cost" in path`` and therefore failed on
        ``PATCH /tenants/{name_or_id}/cost-centre`` — a cost-CENTRE label, which is
        an accounting attribute on the tenant row and records no spend. The
        boundary this class exists to pin is unchanged: no route HERE accrues cost,
        so no gate added in this repo can reach the traffic that does.

        The discriminator is now method + resource shape rather than a substring —
        see ``_is_cost_or_usage_ingestion`` for why ingestion needs both, and
        ``TestTheCostRouteDiscriminatorActuallyDiscriminates`` for the proof that
        narrowing it did not turn it into a predicate that matches nothing.
        """
        cost_routes = _cost_ingestion_routes()
        assert cost_routes == [], (
            "this service has grown a cost/usage ingestion route, which means the "
            "archived-tenant gates now have a population they could cover and this "
            f"test's premise needs re-deciding: {cost_routes}"
        )

    def test_archiving_does_not_touch_billing_rows(
        self, auth_client_admin: TestClient, main_db, licence_on_a_tenant_about_to_be_archived
    ) -> None:
        """⛔ AND MUST NOT. Dropping an archived tenant's cost rows would clear the
        alert by converting an ATTRIBUTION fault into an UNMETERED one — the compute
        is still spent, merely no longer visible, which is strictly worse. Refuse
        upstream; never discard a cost event.
        """
        response = auth_client_admin.patch(
            "/tenants/retired-token-corp/archive", json={"archived": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None
        # The response carries the archive stamp and nothing about spend, because
        # this service neither holds nor adjusts cost rows.
        assert "cost_events" not in response.json()
        assert "deleted" not in response.json()


class TestTheCostRouteDiscriminatorActuallyDiscriminates:
    """⛔ THE CONTROL ON THE NARROWING, and the reason narrowing is safe.

    ``test_this_service_has_no_route_that_records_a_cost_event`` asserts an EMPTY
    list, so it passes trivially if the predicate stops matching anything — the
    failure mode of every "assert the absence" test, and the one a narrowing makes
    likely rather than hypothetical. These cases run the discriminator on synthetic
    shapes so the assertion above cannot become decorative.

    Both halves of the predicate are shown carrying their own weight: ``GET /costs``
    is refused by the METHOD rule with the segment matching, and
    ``POST /tenants/{id}/cost-centre`` is refused by the SEGMENT rule with the
    method matching. Neither rule alone would give the right answers here.
    """

    def test_it_catches_a_real_ingestion_route(self) -> None:
        for method, path in (
            ("POST", "/cost-events"),
            ("POST", "/usage"),
            ("POST", "/tenants/{tenant_id}/usage-events"),
            ("PUT", "/metering"),
            ("POST", "/internal/costs"),
        ):
            assert _is_cost_or_usage_ingestion(method, path), (
                f"{method} {path} is exactly the route whose arrival must re-open "
                "this test's premise, and the discriminator does not see it"
            )

    def test_it_does_not_catch_the_cost_centre_label(self) -> None:
        """The case that was failing. A label on a tenant is not a cost event."""
        assert not _is_cost_or_usage_ingestion("PATCH", "/tenants/{name_or_id}/cost-centre")
        # The SEGMENT rule alone must refuse it, independently of the method —
        # otherwise turning this endpoint into a PUT would silently red this suite
        # for a change that accrues nothing.
        assert not _is_cost_or_usage_ingestion("POST", "/tenants/{name_or_id}/cost-centre")

    def test_a_read_is_not_an_ingestion(self) -> None:
        """The METHOD rule alone must refuse these, with the segment matching."""
        assert not _is_cost_or_usage_ingestion("GET", "/costs")
        assert not _is_cost_or_usage_ingestion("GET", "/tenants/{tenant_id}/usage")

    def test_an_unrelated_write_is_not_an_ingestion(self) -> None:
        assert not _is_cost_or_usage_ingestion("POST", "/licenses")
        assert not _is_cost_or_usage_ingestion("POST", "/licenses/{license_id}/tokens")

    def test_the_live_route_that_provoked_the_narrowing_is_still_present(self) -> None:
        """⚠️ AND IT MUST BE, or the two tests above are grading a route table that
        no longer contains the case they were narrowed around — the narrowing would
        then be unmotivated and nothing would say so."""
        paths = {getattr(route, "path", "") for route in main.app.routes}
        assert "/tenants/{name_or_id}/cost-centre" in paths
