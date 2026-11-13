"""`subject.tenant_id` must be STATED when the database already answers it — and
never guessed when it does not.

WHAT THIS PINS, AND WHY IT IS NOT COVERED BY THE 029/032 TESTS. Migration 029
added the column and backfilled nothing; 032 set the 51 subjects whose parent was
derivable and refused the other 203. Both are one-shot SQL files and both have
tests that pin what those FILES do. Neither pins anything about RUNTIME, because
until now no code path wrote the column at all — which is why the unstated
population regrew: measured on prod 2026-09-14, 8 subjects sat in 032's own
admissible bucket (exactly one licence tenant, not a machine), every one of them
minted after 032 ran.

So these tests pin the runtime writer, and — more importantly — its REFUSALS. A
test that only proved "it sets the column" would pass just as happily on an
implementation that guesses, and guessing a parent is the defect class this whole
column sits inside: an identity attributed to an org it does not belong to is,
downstream, indistinguishable from an operator having decided it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

ORG_TENANT = "02000000-0000-7000-8000-0000000000a1"
OTHER_ORG_TENANT = "02000000-0000-7000-8000-0000000000a2"
MACHINE_TENANT = "02000000-0000-7000-8000-0000000000b1"


@pytest.fixture
def org_tenant(db_manager):
    return db_manager.ensure_tenant(
        tenant_id=ORG_TENANT, name="acme-org", kind="customer", owner_key="platform"
    )


@pytest.fixture
def other_org_tenant(db_manager):
    return db_manager.ensure_tenant(
        tenant_id=OTHER_ORG_TENANT, name="beta-org", kind="customer", owner_key="platform"
    )


@pytest.fixture
def machine_tenant(db_manager):
    return db_manager.ensure_tenant(
        tenant_id=MACHINE_TENANT, name="svc-kag-demo", kind="service", owner_key="platform"
    )


def _licence(db_manager, subject, seed_app, seed_plan, tenant_id: str):
    return db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=tenant_id,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


class TestAdmissionRule:
    def test_sets_when_exactly_one_non_service_tenant(
        self, db_manager, seed_app, seed_plan, org_tenant
    ):
        """The one case that WRITES: one licence, one real org tenant."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-single", keycloak_realm="secure-apps"
        )
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        assert reason == "set"
        assert str(db_manager.get_subject(UUID(str(subject.id))).tenant_id) == ORG_TENANT

    def test_refuses_a_machine_tenant(self, db_manager, seed_app, seed_plan, machine_tenant):
        """⛔ The refusal migration 032 calls its most important decision.

        `svc-kag` the subject -> `svc-kag` the tenant DERIVES cleanly; writing it
        would record "a machine is an org" a second time in the column added to
        express the corrected shape, and launder it as a stated fact. The
        discriminator is the TENANT's kind, never the subject's name.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-kag-demo", keycloak_realm="service"
        )
        _licence(db_manager, subject, seed_app, seed_plan, MACHINE_TENANT)

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=MACHINE_TENANT
        )

        assert reason == "tenant_is_machine"
        assert db_manager.get_subject(UUID(str(subject.id))).tenant_id is None

    def test_a_svc_named_subject_under_a_real_org_is_set(
        self, db_manager, seed_app, seed_plan, org_tenant
    ):
        """The other half of the same discriminator, and the one a name filter breaks.

        17 of the 51 rows 032 set are `svc-*` subjects whose licences already sit
        under the REAL org tenant — the target model already in effect. A
        `name LIKE 'svc-%'` filter on the SUBJECT would exclude all 17 correct
        rows and include none of the 41 wrong ones: exactly backwards. Without
        this test, "refuse svc-* subjects" passes the test above and is wrong.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-workers-acme", keycloak_realm="service"
        )
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        assert reason == "set"

    def test_refuses_a_multi_tenant_subject(
        self, db_manager, seed_app, seed_plan, org_tenant, other_org_tenant
    ):
        """A person may belong to several orgs; one column cannot say so. NULL is honest."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-multi", keycloak_realm="portal"
        )
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)
        _licence(db_manager, subject, seed_app, seed_plan, OTHER_ORG_TENANT)

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        assert reason == "ambiguous"
        assert db_manager.get_subject(UUID(str(subject.id))).tenant_id is None

    def test_a_revoked_licence_under_another_tenant_still_counts(
        self, db_manager, seed_app, seed_plan, org_tenant, other_org_tenant
    ):
        """Counted over ALL licences, not just active ones — 032's own wording.

        Counting only active rows MANUFACTURES certainty: a subject with a revoked
        licence under A and an active one under B reads as unambiguous while the
        database plainly holds two different answers.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-revoked", keycloak_realm="portal"
        )
        revoked = _licence(db_manager, subject, seed_app, seed_plan, OTHER_ORG_TENANT)
        db_manager.revoke_license(UUID(str(revoked.id)))
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        assert reason == "ambiguous"

    def test_refuses_an_unregistered_tenant(self, db_manager, seed_app, seed_plan):
        """The money-bug shape: a Keycloak `sub` forwarded as X-Tenant-Id.

        A `sub` IS UUID-shaped, so nothing about its FORM rejects it. What rejects
        it is that it has no `tenants` row. This is the same authority the mint
        guard uses, re-checked here so the idempotent call site cannot smuggle an
        unregistered id past it.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-unregistered", keycloak_realm="portal"
        )

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)),
            tenant_id="019ef931-cb6f-7204-a775-94521e24b2c5",
        )

        assert reason == "tenant_unknown"
        assert db_manager.get_subject(UUID(str(subject.id))).tenant_id is None

    def test_never_overwrites_a_stated_parent(
        self, db_manager, seed_app, seed_plan, org_tenant, other_org_tenant
    ):
        """A stated parentage is an operator fact; a licence mint is not evidence to revise it.

        Without this, the LAST POST wins and a subject's org silently follows
        whichever tenant provisioned most recently.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-stated", keycloak_realm="portal"
        )
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)
        assert (
            db_manager.attribute_subject_to_tenant(
                subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
            )
            == "set"
        )

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=OTHER_ORG_TENANT
        )

        assert reason == "already_set"
        assert str(db_manager.get_subject(UUID(str(subject.id))).tenant_id) == ORG_TENANT

    def test_no_licences_is_not_a_write(self, db_manager, org_tenant):
        """No licence = no evidence. Attributing on a bare header would be the guess."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="attr-bare", keycloak_realm="portal"
        )

        reason = db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        assert reason == "no_licences"
        assert db_manager.get_subject(UUID(str(subject.id))).tenant_id is None


class TestParentagePartition:
    def test_counts_each_bucket_separately(
        self, db_manager, seed_app, seed_plan, org_tenant, other_org_tenant, machine_tenant
    ):
        """The diagnostic must SEPARATE the repairable bucket from the three that are not.

        A single "how many NULL" number is what made this defect invisible for two
        migrations: 213 unstated reads as a backlog, while the 8 that the database
        can actually answer read as a live regression. Only the split says which.
        """
        resolvable = db_manager.create_or_get_subject(
            keycloak_sub="part-resolvable", keycloak_realm="portal"
        )
        _licence(db_manager, resolvable, seed_app, seed_plan, ORG_TENANT)

        ambiguous = db_manager.create_or_get_subject(
            keycloak_sub="part-ambiguous", keycloak_realm="portal"
        )
        _licence(db_manager, ambiguous, seed_app, seed_plan, ORG_TENANT)
        _licence(db_manager, ambiguous, seed_app, seed_plan, OTHER_ORG_TENANT)

        machine = db_manager.create_or_get_subject(
            keycloak_sub="part-machine", keycloak_realm="service"
        )
        _licence(db_manager, machine, seed_app, seed_plan, MACHINE_TENANT)

        db_manager.create_or_get_subject(keycloak_sub="part-bare", keycloak_realm="portal")

        partition = db_manager.subject_tenant_parentage_partition()

        assert partition["resolvable_unstated"] == 1
        assert partition["ambiguous_unstated"] == 1
        assert partition["service_tenant_unstated"] == 1
        assert partition["no_licences_unstated"] == 1
        assert partition["subjects_total"] == 4
        assert partition["unstated"] == 4
        assert partition["stated"] == 0

    def test_a_stated_subject_leaves_the_unstated_counts(
        self, db_manager, seed_app, seed_plan, org_tenant
    ):
        """`resolvable_unstated` must go to zero once stated, or it can never be a signal."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="part-then-stated", keycloak_realm="portal"
        )
        _licence(db_manager, subject, seed_app, seed_plan, ORG_TENANT)
        assert db_manager.subject_tenant_parentage_partition()["resolvable_unstated"] == 1

        db_manager.attribute_subject_to_tenant(
            subject_id=UUID(str(subject.id)), tenant_id=ORG_TENANT
        )

        partition = db_manager.subject_tenant_parentage_partition()
        assert partition["resolvable_unstated"] == 0
        assert partition["stated"] == 1
        assert partition["unstated"] == 0
