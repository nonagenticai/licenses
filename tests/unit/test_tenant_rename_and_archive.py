"""Tenant rename + archive — the UPDATE and the closest thing to a DELETE.

`PUT /tenants/{id}` is `register_tenant`, an idempotent register-if-absent that
returns an existing row UNCHANGED, so until now there was no way to correct a
tenant name through the API at all: a typo at provisioning time was permanent.
And there is no DELETE, deliberately — a tenant owns licences, tokens, budgets,
workspaces and fr `user_roles` rows.

The load-bearing test in this file is `TestArchivingRevokesNothing`. The
docstrings promise that retiring a tenant does not cut anyone off; a promise
about destructive behaviour that only exists in prose is the kind that gets
falsified in production.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from models import ClientSafeError, Tenant, TenantNameTakenError


def _mk(db_manager, name: str) -> Tenant:
    return db_manager.create_tenant(name=name, owner_key="platform")


class TestRename:
    def test_renames_and_keeps_the_id(self, db_manager):
        # The UUID is the identity; the name is a label. A rename that moved the
        # id would silently orphan every licence, token and budget pointing at it.
        t = _mk(db_manager, "acme-before")
        original_id = str(t.id)
        renamed = db_manager.rename_tenant("acme-before", "acme-after")
        assert renamed is not None
        assert renamed.name == "acme-after"
        assert str(renamed.id) == original_id

    def test_resolves_by_uuid_as_well_as_name(self, db_manager):
        t = _mk(db_manager, "by-uuid")
        renamed = db_manager.rename_tenant(str(t.id), "by-uuid-renamed")
        assert renamed is not None and renamed.name == "by-uuid-renamed"

    def test_renaming_to_the_current_name_is_a_no_op_not_a_conflict(self, db_manager):
        # Otherwise the tenant collides with itself and a retried request 409s.
        _mk(db_manager, "same-name")
        assert db_manager.rename_tenant("same-name", "same-name").name == "same-name"

    def test_a_name_owned_by_another_tenant_is_refused(self, db_manager):
        _mk(db_manager, "taken-already")
        _mk(db_manager, "wants-it")
        with pytest.raises(TenantNameTakenError):
            db_manager.rename_tenant("wants-it", "taken-already")

    def test_an_invalid_name_is_a_client_error_not_an_integrity_error(self, db_manager):
        # The CHECK constraint would reject these anyway — as a 500. Validating
        # here is what makes the caller's mistake legible to the caller.
        _mk(db_manager, "valid-name")
        for bad in ["Has-Capitals", "-leading-hyphen", "has spaces", "has_underscore", ""]:
            with pytest.raises(ClientSafeError):
                db_manager.rename_tenant("valid-name", bad)

    def test_an_unknown_tenant_is_none_not_an_exception(self, db_manager):
        assert db_manager.rename_tenant("no-such-tenant", "whatever") is None

    def test_losing_the_race_to_the_unique_index_is_a_name_conflict_not_an_integrity_error(
        self, db_manager
    ):
        """The name pre-check is racy: a concurrent writer can take the name after
        it and before the commit. The unique index then decides, and its
        IntegrityError must surface as ``TenantNameTakenError`` — the type the
        route maps to 409 — never a raw IntegrityError (500) nor a generic
        ClientSafeError (which the route maps to 422) (#258)."""
        _mk(db_manager, "racer")
        outcome = _race_for_name(
            db_manager,
            "contested-name",
            lambda: db_manager.rename_tenant("racer", "contested-name"),
        )
        exc = outcome["exc"]
        assert type(exc) is TenantNameTakenError, repr(exc)
        assert "already in use" in str(exc)
        # The loser's rename did not land; the winner holds the name.
        assert db_manager.get_tenant("racer") is not None
        assert db_manager.get_tenant("contested-name") is not None


def _race_for_name(db_manager, name: str, call) -> dict:
    """Run ``call`` while another transaction holds an UNCOMMITTED insert of a
    tenant named ``name``, then commit that insert.

    Deterministic: the pre-check cannot see the uncommitted row, so the rename's
    UPDATE blocks on the unique index; we poll pg_stat_activity for that lock wait
    instead of sleeping blind, and only then let the competitor win."""
    blocker = db_manager.get_session()
    outcome: dict = {}

    def _run() -> None:
        try:
            outcome["result"] = call()
            outcome["exc"] = None
        except BaseException as exc:  # noqa: BLE001 -- the caller's assertion inspects it
            outcome["exc"] = exc

    worker = threading.Thread(target=_run)
    try:
        blocker.add(Tenant(id=uuid4(), name=name, owner_key="platform"))
        blocker.flush()  # row exists, uncommitted: invisible to the pre-check
        worker.start()
        deadline = time.monotonic() + 15
        waiting = False
        while time.monotonic() < deadline and not waiting:
            with db_manager.engine.connect() as probe:
                waiting = bool(
                    probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' AND query ILIKE 'UPDATE tenants%'"
                        )
                    ).scalar()
                )
            if not waiting:
                time.sleep(0.05)
        assert waiting, "the rename never blocked on the competing insert"
        blocker.commit()
    finally:
        blocker.close()
        worker.join(15)
    assert not worker.is_alive()
    return outcome


class TestRenameRoute:
    """``PATCH /tenants/{name_or_id}`` — before #258 a taken name was a 500."""

    def test_a_taken_name_is_409_with_a_clear_detail(self, auth_client_admin, db_manager):
        _mk(db_manager, "route-taken")
        _mk(db_manager, "route-wants-it")
        r = auth_client_admin.patch("/tenants/route-wants-it", json={"name": "route-taken"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "tenant_name_taken"
        assert detail["name"] == "route-taken"
        assert "already in use" in detail["message"]
        # Nothing moved.
        assert db_manager.get_tenant("route-wants-it") is not None

    def test_losing_the_race_through_the_route_is_409_not_500_or_422(
        self, auth_client_admin, db_manager
    ):
        _mk(db_manager, "route-racer")
        outcome = _race_for_name(
            db_manager,
            "route-contested",
            lambda: auth_client_admin.patch(
                "/tenants/route-racer", json={"name": "route-contested"}
            ),
        )
        assert outcome["exc"] is None, repr(outcome["exc"])
        r = outcome["result"]
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "tenant_name_taken"
        assert db_manager.get_tenant("route-racer") is not None

    def test_an_invalid_name_is_422(self, auth_client_admin, db_manager):
        _mk(db_manager, "route-valid")
        for bad in ["Has-Capitals", "-leading-hyphen", "has spaces", "has_underscore"]:
            r = auth_client_admin.patch("/tenants/route-valid", json={"name": bad})
            assert r.status_code == 422, (bad, r.text)
        assert db_manager.get_tenant("route-valid") is not None

    def test_control_a_free_name_renames(self, auth_client_admin, db_manager):
        t = _mk(db_manager, "route-free-before")
        r = auth_client_admin.patch("/tenants/route-free-before", json={"name": "route-free-after"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "route-free-after"
        assert r.json()["id"] == str(t.id)


class TestArchive:
    def test_archives_and_restores(self, db_manager):
        t = _mk(db_manager, "retire-me")
        assert db_manager.set_tenant_archived("retire-me", True).archived_at is not None
        # Restored BY ID: since migration 053 a name resolves to the LIVE tenant only.
        assert db_manager.set_tenant_archived("retire-me", False) is None
        assert db_manager.set_tenant_archived(str(t.id), False).archived_at is None

    def test_re_archiving_keeps_the_original_timestamp(self, db_manager):
        # That timestamp answers "when was this retired". A repeated call must
        # not rewrite history — and a retry is the normal case for an API.
        t = _mk(db_manager, "retire-twice")
        first = db_manager.set_tenant_archived("retire-twice", True).archived_at
        again = db_manager.set_tenant_archived(str(t.id), True).archived_at
        assert again == first

    def test_an_unknown_tenant_is_none_not_an_exception(self, db_manager):
        assert db_manager.set_tenant_archived("no-such-tenant", True) is None


class TestListing:
    def test_archived_tenants_are_hidden_by_default(self, db_manager):
        _mk(db_manager, "live-one")
        _mk(db_manager, "hidden-one")
        db_manager.set_tenant_archived("hidden-one", True)
        names = [t.name for t in db_manager.list_tenants()]
        assert "live-one" in names
        assert "hidden-one" not in names

    def test_include_archived_shows_them_again(self, db_manager):
        # Without this, archiving is a ONE-WAY DOOR through the API: nothing can
        # list the tenant, so nothing can un-archive it.
        _mk(db_manager, "restore-me")
        db_manager.set_tenant_archived("restore-me", True)
        names = [t.name for t in db_manager.list_tenants(include_archived=True)]
        assert "restore-me" in names


class TestServiceActorsAreFilteredNotArchived:
    """Service principals are 33 of the 59 live rows on prod (2026-09-03). They
    are NOT tenants in the commercial sense — migration 027 says so in its own
    header — so they do not belong in the operator pick-list.

    They must be FILTERED rather than archived: archiving blocks provisioning
    (that is precisely what this filter does) and their subject ids are pinned
    across gitops `*_ONBEHALF_SUBJECTS` allowlists, so retiring the rows would
    break live on-behalf auth."""

    def test_service_actors_are_hidden_from_the_pick_list_by_default(self, db_manager):
        db_manager.ensure_tenant(
            uuid4(), name="svc-filtered-one", kind="service", owner_key="platform"
        )
        _mk(db_manager, "a-real-tenant")
        names = [t.name for t in db_manager.list_tenants()]
        assert "a-real-tenant" in names
        assert "svc-filtered-one" not in names

    def test_include_service_actors_brings_them_back(self, db_manager):
        # The admin completeness view and any flow that provisions a licence TO a
        # service principal need to see them; without this the filter would be a
        # one-way door in the same way archiving would.
        db_manager.ensure_tenant(
            uuid4(), name="svc-filtered-two", kind="service", owner_key="platform"
        )
        names = [t.name for t in db_manager.list_tenants(include_service_actors=True)]
        assert "svc-filtered-two" in names

    def test_filtering_does_not_archive(self, db_manager):
        # The distinction this whole class exists for. A filtered row is still
        # live: archived_at stays NULL, so provisioning and on-behalf auth are
        # untouched.
        t = db_manager.ensure_tenant(
            uuid4(), name="svc-still-live", kind="service", owner_key="platform"
        )
        assert t.archived_at is None
        fetched = db_manager.get_tenant("svc-still-live")
        assert fetched is not None and fetched.archived_at is None

    def test_the_two_filters_are_independent(self, db_manager):
        # An archived service actor must not reappear just because the caller
        # asked for service actors.
        db_manager.ensure_tenant(
            uuid4(), name="svc-archived-too", kind="service", owner_key="platform"
        )
        db_manager.set_tenant_archived("svc-archived-too", True)
        names = [t.name for t in db_manager.list_tenants(include_service_actors=True)]
        assert "svc-archived-too" not in names
        both = [
            t.name
            for t in db_manager.list_tenants(include_archived=True, include_service_actors=True)
        ]
        assert "svc-archived-too" in both


class TestEnsureTenantStampsKind:
    """A service principal must be born `kind='service'`. Minted without it the
    row defaults to 'unclassified' and lands straight in the operator inventory
    the filter above exists to keep clean — which is how 43 of 76 prod rows came
    to need hand-reclassification."""

    def test_kind_is_applied_on_insert(self, db_manager):
        t = db_manager.ensure_tenant(
            uuid4(), name="svc-born-classified", kind="service", owner_key="platform"
        )
        assert t.kind == "service"

    def test_default_is_unclassified_not_a_guess(self, db_manager):
        # Fail closed: a tenant nobody has decided about must not be silently
        # counted as a customer (inflating revenue) or internal (hiding cost).
        t = db_manager.ensure_tenant(uuid4(), name="undecided-tenant", owner_key="platform")
        assert t.kind == "unclassified"

    def test_an_existing_row_is_not_reclassified(self, db_manager):
        # ensure_tenant is idempotent and an operator's later correction is the
        # authority — the same rule migration 027 states for its own backfill.
        tid = uuid4()
        db_manager.ensure_tenant(tid, name="already-decided", kind="internal", owner_key="platform")
        again = db_manager.ensure_tenant(
            tid, name="already-decided", kind="service", owner_key="platform"
        )
        assert again.kind == "internal"


class TestEnsureTenantStampsByoMode:
    """A machine identity must not be BORN unable to satisfy its own BYO policy.

    `tenants.byo_mode` server-defaults to 'byo_required' — "commercial: must bring
    their own provider; worker_management hard-fails a run with no configured one".
    An `svc-*` service principal has no account and nowhere to have put an API key,
    so on it that is not a strict policy but an unsatisfiable one: the only
    reachable outcome is a 402 `byo_llm_not_configured`, raised WITHOUT LOGGING
    (worker_management/src/api/message_endpoints.py:628). svc-kag took exactly that
    402 on 2026-09-03T04:16:53Z.

    Migration 031 repaired the 34 rows that had accumulated and its header names
    THIS call as the unfixed generator: `provision_service_token.py` could pass
    `kind` but not `byo_mode`, so every principal it minted was born
    kind='service' + byo_mode='byo_required'. These tests pin the generator fix,
    which is what stops the 35th row. 031 deliberately did NOT add a CHECK
    constraint instead — with the server_default still 'byo_required' that would
    have failed the live provisioning path closed, against the ext_authz backing
    store.
    """

    def test_byo_mode_is_applied_on_insert(self, db_manager):
        t = db_manager.ensure_tenant(
            uuid4(),
            name="svc-born-coherent",
            kind="service",
            byo_mode="local_allowed",
            owner_key="platform",
        )
        assert t.byo_mode == "local_allowed"

    def test_the_default_is_byo_required_for_a_real_tenant(self, db_manager):
        """A non-service tenant with no explicit byo_mode gets the column default,
        'byo_required' since migration 058 (licenses#273, owner ruling 2026-10-15:
        new early-access tenants bring their own tokens). It was 'local_allowed'
        under 042."""
        t = db_manager.ensure_tenant(uuid4(), name="commercial-tenant", owner_key="platform")
        assert t.byo_mode == "byo_required"

    def test_an_existing_row_keeps_its_byo_mode(self, db_manager):
        """INSERT-only, exactly like `kind`. Re-running the provisioning script
        must never overwrite an operator's later choice (027, 030, 031)."""
        tid = uuid4()
        db_manager.ensure_tenant(
            tid,
            name="svc-already-set",
            kind="service",
            byo_mode="platform_only",
            owner_key="platform",
        )
        again = db_manager.ensure_tenant(
            tid,
            name="svc-already-set",
            kind="service",
            byo_mode="local_allowed",
            owner_key="platform",
        )
        assert again.byo_mode == "platform_only"

    def test_an_invalid_value_is_refused_here_not_by_the_constraint(self, db_manager):
        """ClientSafeError naming the rule, rather than an opaque IntegrityError
        out of ck_tenants_byo_mode (021) — matching set_tenant_byo_mode."""
        with pytest.raises(ClientSafeError, match="invalid byo_mode"):
            db_manager.ensure_tenant(
                uuid4(), name="svc-bad-mode", byo_mode="byo_optional", owner_key="platform"
            )

    def test_no_tenant_row_is_created_when_the_value_is_invalid(self, db_manager):
        """The validation must happen BEFORE the insert, or a rejected call still
        leaves a half-provisioned principal behind."""
        tid = uuid4()
        with pytest.raises(ClientSafeError):
            db_manager.ensure_tenant(
                tid, name="svc-never-created", byo_mode="nonsense", owner_key="platform"
            )
        assert db_manager.get_tenant(str(tid)) is None


class TestTheProvisioningScriptPassesIt:
    """The generator fix is only real at the CALL SITE. A parameter nobody passes
    changes nothing — the 402 comes from what provision_service_token.py mints."""

    def test_provision_service_token_mints_a_coherent_service_principal(self):
        src = (
            Path(__file__).resolve().parents[2] / "scripts" / "provision_service_token.py"
        ).read_text(encoding="utf-8")
        call = src.split("db.ensure_tenant(", 1)[1].split(")", 1)[0]
        assert 'kind="service"' in call, "the principal must be born kind='service' (027)"
        assert 'byo_mode="local_allowed"' in call, (
            "the principal must be born byo_mode='local_allowed' — otherwise the "
            "server_default 'byo_required' makes it 402 on its first BYO-gated "
            "call, which is the generator migration 031's header names"
        )


class TestArchivingRevokesNothing:
    """The promise the docstrings make, asserted rather than described.

    Archiving is the closest thing this API has to a delete, so the tempting
    implementation is one that ALSO tidies up the tenant's licences. That would
    turn "retire this tenant from the picker" into "cut off everyone using it" —
    silently, and with no separate audit trail. The whole point of a soft delete
    is that it is reversible and inert.
    """

    def test_licences_and_tokens_survive_an_archive(self, db_manager):
        tenant = _mk(db_manager, "still-working")
        app = db_manager.create_app(
            key="archive-probe-app", name="Archive Probe", issuers=("platform",)
        )
        plan = db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
        subject = db_manager.create_or_get_subject(
            keycloak_sub="11111111-1111-4111-8111-111111111111",
            keycloak_realm="secure-apps",
        )
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=UUID(str(app.id)),
            plan_id=UUID(str(plan.id)),
            tenant_id=str(tenant.id),
        )
        token, _secret = db_manager.create_token(license_id=UUID(str(lic.id)))

        db_manager.set_tenant_archived("still-working", True)

        after = db_manager.get_license(UUID(str(lic.id)))
        assert after is not None, "archiving the tenant destroyed its licence"
        assert after.revoked_at is None, "archiving the tenant revoked its licence"

        # No public "tokens for a licence" accessor, so read the table directly —
        # the claim under test is about STORED state, and going through a
        # convenience method would only prove the method agrees with itself.
        session = db_manager.get_session()
        try:
            from models import Token

            live = (
                session.query(Token)
                .filter(Token.license_id == lic.id, Token.revoked_at.is_(None))
                .all()
            )
        finally:
            session.close()
        assert len(live) == 1, "archiving the tenant revoked a live token"
        assert str(token.id) in {str(t.id) for t in live}

    def test_the_tenant_itself_is_still_resolvable_by_id(self, db_manager):
        # license-authz resolves a tenant on every authz decision. If archiving
        # made the row unresolvable, every in-flight request for that tenant
        # would start failing — which is precisely the cut-off this must not be.
        t = _mk(db_manager, "still-resolvable")
        db_manager.set_tenant_archived("still-resolvable", True)
        assert db_manager.get_tenant(str(t.id)) is not None
        # By NAME it is not (migration 053): names are unique among LIVE tenants
        # only, so several archived rows may share one and a name cannot pick
        # among them. license-authz already keys on the id (licence.tenant_id) and
        # its name map comes from /internal/tenants, which excludes archived rows.
        assert db_manager.get_tenant("still-resolvable") is None

    def test_a_live_token_stops_authenticating_through_introspect_after_archive(
        self, db_manager, client
    ):
        """⭐ THIS TEST WAS DELIBERATELY INVERTED. Read this before "fixing" it.

        It used to assert the OPPOSITE — that `POST /license/introspect` keeps
        answering `active: true` for a token whose tenant was just archived — and
        it said so on purpose: "A promise pinned only at the model layer could
        still be silently broken by the route (e.g. a filter added to
        `introspect_token` that checks `tenant.archived_at`); this is what would
        catch that." It caught exactly that. The filter was then added ANYWAY,
        as a reviewed decision, because the behaviour it protected was the
        defect: the registry had no working off-switch. A tenant could be
        retired and keep authorizing spend indefinitely.

        The same docstring named the gap it was holding open — "a real
        offboarding flow (archive + revoke every live licence, in one auditable
        act) is owed and does not exist yet". That gap is now closed by
        REFUSING rather than revoking, which is why the rest of this class is
        UNCHANGED and still passes: archiving still writes no `revoked_at`,
        destroys no row, and is still perfectly reversible. It simply no longer
        authorizes while it is set.

        ⛔ So the class name is still literally true — archiving revokes nothing.
        What it no longer does is nothing.

        ⚠️ This does NOT mean an archived tenant cannot spend. Measured on prod
        2026-09-10, `rmt-e2e` accrued 405 cost events / $1.6018 in the week after
        it was archived, all with `license_id = NULL`, on a path that never
        introspects. See tests/unit/test_archived_tenant_gates_spend.py.
        """
        tenant = _mk(db_manager, "still-authenticating")
        app = db_manager.create_app(
            key="archive-introspect-app", name="Archive Introspect App", issuers=("platform",)
        )
        plan = db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
        subject = db_manager.create_or_get_subject(
            keycloak_sub="22222222-2222-4222-8222-222222222222",
            keycloak_realm="secure-apps",
        )
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=UUID(str(app.id)),
            plan_id=UUID(str(plan.id)),
            tenant_id=str(tenant.id),
        )
        _token, raw = db_manager.create_token(license_id=UUID(str(lic.id)))

        before = client.post("/license/introspect", json={"token": raw})
        assert before.status_code == 200
        assert before.json()["active"] is True, "sanity: the token must authenticate BEFORE archive"

        db_manager.set_tenant_archived("still-authenticating", True)

        after = client.post("/license/introspect", json={"token": raw})
        # Still 200: introspection SUCCEEDED and its answer is "not active". A
        # 4xx here would be a different contract and would break callers that
        # (correctly) treat a non-200 as an infrastructure fault to retry.
        assert after.status_code == 200, after.text
        assert after.json()["active"] is False, (
            "archiving the tenant left its licence authorizing — the registry "
            "still has no working off-switch"
        )
        assert after.json()["reason"] == "tenant_archived", (
            "the refusal must name archiving as the cause, or an operator cannot "
            "tell it from an expired licence or a revoked token"
        )
        # And the STORED state is untouched — the promise the rest of this class
        # makes. Asserted here too because refusal and revocation are exactly the
        # two things this change must keep apart.
        still_there = db_manager.get_license(UUID(str(lic.id)))
        assert still_there is not None and still_there.revoked_at is None, (
            "the refusal revoked the licence — archiving must stay reversible"
        )
        # Reversible in EFFECT, not merely in data: un-archive and it authorizes
        # again. Without this the refusal could be caused by anything the archive
        # call happened to touch.
        db_manager.set_tenant_archived(str(tenant.id), False)  # by id: archived (053)
        restored = client.post("/license/introspect", json={"token": raw})
        assert restored.json()["active"] is True, "un-archiving did not restore the licence"
        assert restored.json()["tenant_id"] == str(tenant.id)
