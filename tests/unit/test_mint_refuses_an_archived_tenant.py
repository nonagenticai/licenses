"""A licence may not be minted against an ARCHIVED tenant.

Measured on `origin/main` before this fix: the mint guard in `create_user_license`
refused only an UNREGISTERED tenant (`get_tenant(...) is None`). `get_tenant` has
no `archived_at` filter — unlike its sibling `list_tenants`, which has had one
since migration 023 — so an archived tenant resolved normally and backed a brand
new, live, billable licence. 17 tenants were archived on 2026-09-04, so the path
was live, not theoretical.

`set_tenant_archived` is this API's closest thing to a delete and `list_tenants`
already hides archived rows from the picker. Minting against one therefore created
a licence on a tenant the operator had retired and could no longer see in any
listing.

Why the gate is on the MINT PATH and not inside `get_tenant`
-----------------------------------------------------------
`db_manager.get_tenant` has 10 production call sites and exactly ONE is a mint.
Filtering inside the shared getter would silently break the other nine:

* `auth/tenant.py` resolves a tenant on the AUTHZ path for EVERY request —
  hiding the row turns "retired from the picker" into "every in-flight request
  for that tenant starts failing", which is the cut-off
  `test_tenant_rename_and_archive.py::TestArchivingRevokesNothing` exists to
  forbid and `test_the_tenant_itself_is_still_resolvable_by_id` pins directly;
* `create_tenant`'s two existence checks (by id, by name) would stop seeing an
  archived row and mint a DUPLICATE tenant over it;
* the read routes (`GET /tenants/{name_or_id}`, the byo-mode lookups, the
  caller's own-tenant read) are how an operator INSPECTS what they just
  archived — filtering there makes an archived tenant un-inspectable through the
  very API that archived it.

So the getter keeps its meaning and the WRITE path states its own precondition.
`TestTheSharedGetterKeptItsMeaning` below is what keeps that decision honest: it
fails if someone later "simplifies" the fix by moving the filter down.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import main
from models import License

ARCHIVED_TENANT = "01000000-0000-7000-8000-0000000000a1"
LIVE_TENANT = "01000000-0000-7000-8000-0000000000a2"


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


def _licences_for(db_manager, tenant_id: str) -> list[License]:
    session = db_manager.get_session()
    try:
        return session.query(License).filter(License.tenant_id == UUID(tenant_id)).all()
    finally:
        session.close()


def _mint(client: TestClient, subject: str, tenant_id: str, seed_app, seed_plan):
    return client.post(
        f"/licenses/user/{subject}",
        json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
        headers={"X-Tenant-Id": tenant_id},
    )


class TestMintRefusesAnArchivedTenant:
    """The defect. On origin/main every assertion here measured a 201."""

    def test_an_archived_tenant_cannot_back_a_new_licence(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        response = _mint(
            auth_client_admin,
            "019e00aa-0000-7000-8000-0000000000b1",
            ARCHIVED_TENANT,
            seed_app,
            seed_plan,
        )

        assert response.status_code == 400, response.text
        assert "archived" in response.json()["detail"].lower(), response.text

    def test_the_refusal_writes_no_licence_row(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        """A 400 that still persisted the row would be the worst outcome: the
        caller is told no and the tenant is billed anyway. The guard runs BEFORE
        `create_license`, and this is what proves it."""
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        _mint(
            auth_client_admin,
            "019e00aa-0000-7000-8000-0000000000b2",
            ARCHIVED_TENANT,
            seed_app,
            seed_plan,
        )

        assert _licences_for(main_db, ARCHIVED_TENANT) == [], (
            "the mint was refused but a licence row was written anyway"
        )

    def test_the_refusal_is_distinguishable_from_an_unregistered_tenant(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        """Two different operator actions fix these: REGISTER the tenant, versus
        UN-ARCHIVE it. One shared message would send the operator to POST /tenants
        for a tenant that already exists — and `create_tenant` would then answer
        409, leaving them stuck with no correct next step."""
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        archived = _mint(
            auth_client_admin,
            "019e00aa-0000-7000-8000-0000000000b3",
            ARCHIVED_TENANT,
            seed_app,
            seed_plan,
        )
        unregistered = _mint(
            auth_client_admin,
            "019e00aa-0000-7000-8000-0000000000b4",
            str(uuid4()),
            seed_app,
            seed_plan,
        )

        assert archived.status_code == unregistered.status_code == 400
        assert archived.json()["detail"] != unregistered.json()["detail"], (
            "an archived tenant and an unregistered one need different remedies"
        )
        assert "not a registered tenant" in unregistered.json()["detail"]

    def test_un_archiving_lets_the_mint_through_again(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        """Archiving is a REVERSIBLE soft delete, so the refusal must lift when the
        operator reverses it — otherwise the fix turns a soft delete into a hard
        one."""
        subject = "019e00aa-0000-7000-8000-0000000000b5"
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        refused = _mint(auth_client_admin, subject, ARCHIVED_TENANT, seed_app, seed_plan)
        assert refused.status_code == 400, refused.text

        # Restored by id: since migration 053 a name resolves to LIVE tenants only.
        main_db.set_tenant_archived(ARCHIVED_TENANT, False)
        allowed = _mint(auth_client_admin, subject, ARCHIVED_TENANT, seed_app, seed_plan)

        assert allowed.status_code == 201, allowed.text


class TestANormalTenantStillMints:
    """The control. A guard that refuses everything also passes every test above,
    so the non-archived path is asserted explicitly."""

    def test_a_live_registered_tenant_mints_normally(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        main_db.ensure_tenant(LIVE_TENANT, name="acme-live", owner_key="platform")

        response = _mint(
            auth_client_admin,
            "019e00aa-0000-7000-8000-0000000000c1",
            LIVE_TENANT,
            seed_app,
            seed_plan,
        )

        assert response.status_code == 201, response.text
        assert len(_licences_for(main_db, LIVE_TENANT)) == 1

    def test_the_seed_tenant_path_is_unaffected(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        """The conftest's default tenant — the one every other endpoint test mints
        against — must be untouched by this guard."""
        response = auth_client_admin.post(
            "/licenses/user/019e00aa-0000-7000-8000-0000000000c2",
            json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
        )

        assert response.status_code == 201, response.text


class TestArchivingAnExistingLicencesTenantChangesNothing:
    """`TestArchivingRevokesNothing` promises archiving revokes nothing. This fix
    blocks NEW licences only; it must not retroactively disturb existing ones."""

    def test_an_existing_licence_survives_its_tenant_being_archived(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        subject = "019e00aa-0000-7000-8000-0000000000d1"
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        minted = _mint(auth_client_admin, subject, ARCHIVED_TENANT, seed_app, seed_plan)
        assert minted.status_code == 201, minted.text

        main_db.set_tenant_archived("retired-corp", True)

        rows = _licences_for(main_db, ARCHIVED_TENANT)
        assert len(rows) == 1
        assert rows[0].status == "active", "archiving the tenant deactivated its licence"
        assert rows[0].revoked_at is None, "archiving the tenant revoked its licence"

    def test_re_provisioning_an_existing_licence_still_returns_it(
        self, auth_client_admin: TestClient, main_db, seed_app, seed_plan
    ):
        """⛔ The load-bearing ordering. portal-bff POSTs this route on EVERY
        session, and the idempotency return sits ABOVE the mint guard on purpose —
        the guard blocks the creation of new rows, never a caller reading back
        state that predates it.

        Putting the archived check above that return (or inside `get_tenant`, which
        the return's own `find_active_license` does not use but the guard does)
        would start 400ing every session refresh for a tenant that was archived
        after its licences were issued. That is a live-traffic outage wearing the
        costume of a security fix.
        """
        subject = "019e00aa-0000-7000-8000-0000000000d2"
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        first = _mint(auth_client_admin, subject, ARCHIVED_TENANT, seed_app, seed_plan)
        assert first.status_code == 201, first.text

        main_db.set_tenant_archived("retired-corp", True)
        again = _mint(auth_client_admin, subject, ARCHIVED_TENANT, seed_app, seed_plan)

        assert again.status_code == 201, again.text
        assert again.json()["id"] == first.json()["id"], (
            "the idempotent re-provision must return the SAME licence, not mint a second"
        )
        assert len(_licences_for(main_db, ARCHIVED_TENANT)) == 1


class TestTheSharedGetterKeptItsMeaning:
    """The blast-radius decision, asserted so it cannot be undone by a later
    "simplification" that moves the filter into `db_manager.get_tenant`.

    Nine of its ten production call sites are reads or conflict-checks that need
    an archived tenant to stay resolvable. If someone adds the filter there, these
    fail — and so does `test_tenant_rename_and_archive.py`'s
    `test_the_tenant_itself_is_still_resolvable_by_id`.

    ⚠️ BY ID, not by name, since migration 053: names are unique per owner among
    LIVE tenants only, so several archived rows may share a name and a name
    resolves to the live row or to nothing. Conflict checks follow the same rule
    (an archived tenant no longer blocks its name). license-authz keys on the id
    and builds its name map from /internal/tenants, which excludes archived rows.
    """

    def test_get_tenant_still_resolves_an_archived_tenant_by_id_not_by_name(self, main_db):
        tenant = main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        assert main_db.get_tenant(str(tenant.id)) is not None, (
            "license-authz resolves a tenant on every authz decision; hiding the row "
            "fails every in-flight request for that tenant"
        )
        assert main_db.get_tenant("retired-corp") is None

    def test_the_read_route_can_still_inspect_an_archived_tenant(
        self, auth_client_admin: TestClient, main_db
    ):
        """The archive tooling's own read-back. If GET /tenants/{id} 404'd on an
        archived tenant, the operator could not verify the archive they just
        performed."""
        main_db.ensure_tenant(ARCHIVED_TENANT, name="retired-corp", owner_key="platform")
        main_db.set_tenant_archived("retired-corp", True)

        response = auth_client_admin.get(f"/tenants/{ARCHIVED_TENANT}")

        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None, (
            "the read must SHOW the archived state, not hide the row"
        )
