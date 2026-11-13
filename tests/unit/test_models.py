"""
Unit tests for SQLAlchemy models.

Tests model behavior, to_dict() methods, is_active()/is_valid() methods,
and constraints.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from models import AllTokensRevokedError, Subject, Token
from tests.conftest import insert_legacy_perpetual_token


class TestRegionModel:
    """Tests for Region model."""

    def test_region_creation(self, db_manager):
        """Test creating a region."""
        region = db_manager.create_region(code="eu", name="Europe", active=True)

        assert region.id is not None
        assert region.code == "eu"
        assert region.name == "Europe"
        assert region.active is True

    def test_region_to_dict(self, seed_region):
        """Test Region.to_dict() method."""
        result = seed_region.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(seed_region.id)
        assert result["code"] == seed_region.code
        assert result["name"] == seed_region.name
        assert result["active"] == seed_region.active
        assert "created_at" in result

    def test_region_unique_code_constraint(self, db_manager, seed_region):
        """Test that region code must be unique."""
        with pytest.raises(IntegrityError):
            db_manager.create_region(code=seed_region.code, name="Duplicate")

    def test_region_get_by_code(self, db_manager, seed_region):
        """Test getting region by code."""
        found = db_manager.get_region_by_code(seed_region.code)
        assert found is not None
        assert found.id == seed_region.id

    def test_region_list_active_only(self, db_manager):
        """Test listing only active regions."""
        active = db_manager.create_region(code="active1", name="Active 1", active=True)
        inactive = db_manager.create_region(code="inactive1", name="Inactive 1", active=False)

        active_regions = db_manager.list_regions(active_only=True)
        assert len(active_regions) == 1
        assert active_regions[0].id == active.id
        assert all(r.active for r in active_regions)

        all_regions = db_manager.list_regions(active_only=False)
        assert len(all_regions) == 2
        all_ids = {r.id for r in all_regions}
        assert active.id in all_ids
        assert inactive.id in all_ids


class TestSubjectModel:
    """Tests for Subject model."""

    def test_subject_creation(self, db_manager):
        """Test creating a subject."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="new-keycloak-sub", keycloak_realm="test-realm"
        )

        assert subject.id is not None
        assert subject.keycloak_sub == "new-keycloak-sub"
        assert subject.keycloak_realm == "test-realm"

    def test_subject_to_dict(self, seed_subject):
        """Test Subject.to_dict() method."""
        result = seed_subject.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(seed_subject.id)
        assert result["keycloak_sub"] == seed_subject.keycloak_sub
        assert result["keycloak_realm"] == seed_subject.keycloak_realm
        assert "created_at" in result

    def test_subject_get_or_create_existing(self, db_manager, seed_subject):
        """Test create_or_get returns existing subject."""
        # Get the same subject again
        found = db_manager.create_or_get_subject(
            keycloak_sub=seed_subject.keycloak_sub,
            keycloak_realm="any-realm",  # Realm ignored for existing
        )
        assert found.id == seed_subject.id

    def test_subject_get_by_keycloak_sub(self, db_manager, seed_subject):
        """Test getting subject by keycloak_sub."""
        found = db_manager.get_subject_by_keycloak_sub(seed_subject.keycloak_sub)
        assert found is not None
        assert found.id == seed_subject.id


class TestCreateOrGetSubjectRace:
    """``create_or_get_subject`` must survive a concurrent first-login.

    ``Subject.keycloak_sub`` is UNIQUE and this sits on the licence-mint hot
    path (``main.create_user_license``, whose docstring notes portal-bff
    provisions on EVERY session) with ``replicas: 2`` behind Envoy. Two
    bootstraps for a brand-new user can both SELECT-miss and both INSERT; the
    loser's UniqueViolation used to propagate to the generic 500 handler, so a
    user's very first login 500'd. The identical race on ``License`` is already
    handled in ``main.create_user_license``; this is the subject-level twin.
    """

    def test_losing_the_insert_race_returns_the_winning_subject(
        self, db_manager, db_engine, monkeypatch
    ):
        """A competitor commits the same keycloak_sub between our SELECT and our COMMIT.

        Deterministic by construction (a seam on ``get_session``) rather than
        hoping two threads interleave — the loser branch is exercised on EVERY
        run, so this test cannot silently stop covering the fix.
        """
        from sqlalchemy import text

        keycloak_sub = "race-first-login-deterministic"
        winner_id = uuid4()
        real_get_session = db_manager.get_session
        raced = {"done": False}

        def racing_get_session():
            session = real_get_session()
            real_commit = session.commit

            def commit_after_a_competitor_wins():
                # Fires exactly once, on the INSERT commit: an independent
                # connection (i.e. the other replica) commits the same
                # keycloak_sub first. Our SELECT has already missed.
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
                            {"id": str(winner_id), "sub": keycloak_sub, "realm": "secure-apps"},
                        )
                return real_commit()

            session.commit = commit_after_a_competitor_wins
            return session

        monkeypatch.setattr(db_manager, "get_session", racing_get_session)

        # No exception, and we get the row that WON — not a 500.
        subject = db_manager.create_or_get_subject(
            keycloak_sub=keycloak_sub, keycloak_realm="secure-apps"
        )

        assert raced["done"] is True, "seam never fired — the race was not simulated"
        assert str(subject.id) == str(winner_id)
        assert subject.keycloak_sub == keycloak_sub

        # Exactly one row survives, and the losing session left no debris.
        session = real_get_session()
        try:
            rows = session.query(Subject).filter(Subject.keycloak_sub == keycloak_sub).all()
            assert len(rows) == 1
            assert str(rows[0].id) == str(winner_id)
        finally:
            session.close()

    def test_session_is_usable_after_losing_the_race(self, db_manager, db_engine, monkeypatch):
        """The re-SELECT must happen on a ROLLED-BACK session.

        A failed flush poisons the transaction — PostgreSQL rejects every
        subsequent statement with InFailedSqlTransaction until it is rolled
        back. Returning the winner therefore only works if the handler rolls
        back FIRST; this asserts the returned row is fully populated (i.e. the
        re-query genuinely executed and returned real column data).
        """
        from sqlalchemy import text

        keycloak_sub = "race-first-login-poisoned-tx"
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
                                "realm": "winning-realm",
                            },
                        )
                return real_commit()

            session.commit = commit_after_a_competitor_wins
            return session

        monkeypatch.setattr(db_manager, "get_session", racing_get_session)

        subject = db_manager.create_or_get_subject(
            keycloak_sub=keycloak_sub, keycloak_realm="losing-realm"
        )

        # Attribute access on the returned (now detached) row proves the
        # re-SELECT ran to completion on a healthy transaction.
        assert subject.id is not None
        assert subject.keycloak_sub == keycloak_sub
        assert subject.keycloak_realm == "winning-realm"
        assert subject.created_at is not None

    def test_two_concurrent_threads_agree_on_one_subject(self, db_manager):
        """Genuine concurrency: two real threads, one keycloak_sub, one row.

        Asserts the INVARIANT (no exception, single id, single row) rather than
        timing, so it can only ever fail for a real reason — a lost race that
        was not absorbed.
        """
        import threading

        keycloak_sub = "race-first-login-threads"
        barrier = threading.Barrier(2, timeout=30)
        results: list[str] = []
        errors: list[BaseException] = []

        def bootstrap():
            try:
                barrier.wait()
                subject = db_manager.create_or_get_subject(
                    keycloak_sub=keycloak_sub, keycloak_realm="secure-apps"
                )
                results.append(str(subject.id))
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=bootstrap) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], f"concurrent first-login raised: {errors!r}"
        assert len(results) == 2
        assert len(set(results)) == 1, f"two threads minted different subjects: {results}"

        session = db_manager.get_session()
        try:
            rows = session.query(Subject).filter(Subject.keycloak_sub == keycloak_sub).all()
            assert len(rows) == 1
        finally:
            session.close()


class TestAppModel:
    """Tests for App model."""

    def test_app_creation(self, db_manager):
        """Test creating an app."""
        app = db_manager.create_app(
            key="new-app",
            name="New Application",
            description="A new test app",
            issuers=("platform",),
        )

        assert app.id is not None
        assert app.key == "new-app"
        assert app.name == "New Application"
        assert app.revoked is False

    def test_app_to_dict(self, seed_app):
        """Test App.to_dict() method."""
        result = seed_app.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(seed_app.id)
        assert result["key"] == seed_app.key
        assert result["name"] == seed_app.name
        assert result["description"] == seed_app.description
        assert result["revoked"] == seed_app.revoked
        assert "created_at" in result

    def test_app_unique_key_constraint(self, db_manager, seed_app):
        """Test that app key must be unique."""
        with pytest.raises(IntegrityError):
            db_manager.create_app(key=seed_app.key, name="Duplicate App", issuers=("platform",))

    def test_app_get_by_key(self, db_manager, seed_app):
        """Test getting app by key."""
        found = db_manager.get_app_by_key(seed_app.key)
        assert found is not None
        assert found.id == seed_app.id

    def test_app_list(self, db_manager, seed_app):
        """Test listing apps returns exactly the apps created."""
        apps = db_manager.list_apps()
        assert len(apps) == 1
        assert apps[0].id == seed_app.id
        assert apps[0].key == seed_app.key


class TestPlanModel:
    """Tests for Plan model."""

    def test_plan_creation(self, db_manager, seed_app):
        """Test creating a plan."""
        plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="premium",
            name="Premium Plan",
            description="Full features",
            duration_days=365,
        )

        assert plan.id is not None
        assert plan.code == "premium"
        assert plan.duration_days == 365
        assert plan.app_id == seed_app.id
        assert plan.name == "Premium Plan"
        assert plan.description == "Full features"
        assert plan.rate_limit_rpm == 60
        assert plan.rate_limit_burst == 10

    def test_plan_to_dict(self, seed_plan):
        """Test Plan.to_dict() method."""
        result = seed_plan.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(seed_plan.id)
        assert result["app_id"] == str(seed_plan.app_id)
        assert result["code"] == seed_plan.code
        assert result["name"] == seed_plan.name
        assert result["rate_limit_rpm"] == seed_plan.rate_limit_rpm
        assert result["rate_limit_burst"] == seed_plan.rate_limit_burst
        assert "created_at" in result

    def test_plan_global_plan_null_region(self, db_manager, seed_app):
        """Test creating a global plan with no region."""
        plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="global",
            name="Global Plan",
            region_id=None,  # Global plan
        )

        assert plan.region_id is None
        result = plan.to_dict()
        assert result["region_id"] is None

    def test_plan_list_by_app(self, db_manager, seed_app, seed_plan):
        """Test listing plans by app filters correctly."""
        # Create a second app with its own plan
        other_app = db_manager.create_app(
            key="other-app",
            name="Other Application",
            description="Another app",
            issuers=("platform",),
        )
        other_plan = db_manager.create_plan(
            app_id=UUID(str(other_app.id)),
            code="other-plan",
            name="Other Plan",
        )

        plans = db_manager.list_plans(app_id=UUID(str(seed_app.id)))
        assert len(plans) == 1
        assert plans[0].id == seed_plan.id
        assert plans[0].app_id == seed_app.id
        # Other app's plan excluded
        assert not any(p.id == other_plan.id for p in plans)


class TestLicenseModel:
    """Tests for License model."""

    def test_license_creation(self, db_manager, seed_subject, seed_app, seed_plan):
        """Test creating a license."""
        license_obj = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            # tenant_id is a real FK to tenants.id (migration 016); use the
            # autoregistered seed tenant. It is a UUID column, so str() to compare.
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="active",
        )

        assert license_obj.id is not None
        assert license_obj.status == "active"
        assert str(license_obj.tenant_id) == "01000000-0000-7000-8000-000000000001"
        assert license_obj.app_id == seed_app.id
        assert license_obj.plan_id == seed_plan.id
        assert license_obj.subject_id == seed_subject.id
        assert license_obj.starts_at is not None
        assert license_obj.created_at is not None

    def test_active_license_uniqueness(self, db_manager, seed_subject, seed_app, seed_plan):
        """At most one ACTIVE license per (tenant, app, subject) — the partial
        unique index rejects a second active row for the same key."""
        from sqlalchemy.exc import IntegrityError

        kwargs = dict(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="active",
        )
        first = db_manager.create_license(**kwargs)
        with pytest.raises(IntegrityError):
            db_manager.create_license(**kwargs)

        # find_active_license returns the single active row.
        found = db_manager.find_active_license(
            tenant_id=kwargs["tenant_id"], app_id=kwargs["app_id"], subject_id=kwargs["subject_id"]
        )
        assert found is not None
        assert found.id == first.id

    def test_revoked_license_does_not_block_new_active(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """A revoked license for the key does NOT count — a fresh active is allowed
        (the index is partial on status='active')."""
        kwargs = dict(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000002",
        )
        revoked = db_manager.create_license(status="revoked", **kwargs)
        active = db_manager.create_license(status="active", **kwargs)
        assert revoked.id != active.id
        found = db_manager.find_active_license(
            tenant_id=kwargs["tenant_id"], app_id=kwargs["app_id"], subject_id=kwargs["subject_id"]
        )
        assert found.id == active.id

    def test_license_to_dict(self, seed_license):
        """Test License.to_dict() method."""
        result = seed_license.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(seed_license.id)
        assert result["subject_id"] == str(seed_license.subject_id)
        assert result["tenant_id"] == str(seed_license.tenant_id)
        assert result["status"] == seed_license.status
        assert "starts_at" in result
        assert "expires_at" in result
        assert "metadata" in result

    def test_license_is_active_wrong_status(self, db_manager, seed_subject, seed_app, seed_plan):
        """Test is_active() returns False for revoked license created via DB."""
        license_obj = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="revoked",
        )
        assert license_obj.is_active() is False

    def test_license_is_active_status_logic(self, db_manager, seed_subject, seed_app, seed_plan):
        """Test is_active() returns False for non-active statuses using DB-created licenses."""
        pending = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="pending",
        )
        assert pending.is_active() is False

        expired = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="expired",
        )
        assert expired.is_active() is False

    def test_license_is_active_false_wrong_status(self, seed_revoked_license):
        """Test is_active() returns False for revoked license."""
        assert seed_revoked_license.is_active() is False

    def test_license_list_by_tenant(self, db_manager, seed_license, seed_other_tenant_license):
        """Test listing licenses filters by tenant."""
        licenses = db_manager.list_licenses(tenant_id="01000000-0000-7000-8000-000000000001")
        assert len(licenses) == 1
        assert licenses[0].id == seed_license.id
        assert str(licenses[0].tenant_id) == "01000000-0000-7000-8000-000000000001"
        # Other tenant's license excluded
        assert not any(lic.id == seed_other_tenant_license.id for lic in licenses)

        # Verify other tenant has its own license
        other_licenses = db_manager.list_licenses(tenant_id="01000000-0000-7000-8000-000000000002")
        assert len(other_licenses) == 1
        assert other_licenses[0].id == seed_other_tenant_license.id

    def test_get_active_license_match(self, db_manager, seed_license, seed_app):
        """get_active_license finds the active license for (tenant, app_key)."""
        found = db_manager.get_active_license(
            tenant_id="01000000-0000-7000-8000-000000000001", app_key="test-app"
        )
        assert len(found) == 1
        assert found[0].id == seed_license.id

    def test_get_active_license_wrong_tenant(self, db_manager, seed_license, seed_app):
        """No match when tenant doesn't own a license for this app."""
        # A registered-but-unrelated tenant (tenant_id is a UUID column since
        # migration 016; a non-UUID string like "nope" would raise, not return []).
        assert (
            db_manager.get_active_license(
                tenant_id="01000000-0000-7000-8000-000000000002", app_key="test-app"
            )
            == []
        )

    def test_get_active_license_wrong_app_key(self, db_manager, seed_license):
        """No match for unknown app_key."""
        assert (
            db_manager.get_active_license(
                tenant_id="01000000-0000-7000-8000-000000000001", app_key="ghost"
            )
            == []
        )

    def test_get_active_license_excludes_revoked(self, db_manager, seed_revoked_license):
        """Revoked licenses are excluded."""
        assert (
            db_manager.get_active_license(
                tenant_id="01000000-0000-7000-8000-000000000001", app_key="test-app"
            )
            == []
        )

    def test_license_revoke(self, db_manager, seed_license):
        """Test revoking a license."""
        success = db_manager.revoke_license(UUID(str(seed_license.id)))
        assert success is True

        # Fetch again to verify
        revoked = db_manager.get_license(UUID(str(seed_license.id)))
        assert revoked.status == "revoked"
        assert revoked.revoked_at is not None

    def test_license_metadata(self, db_manager, seed_subject, seed_app, seed_plan):
        """Test license metadata storage with round-trip verification."""
        metadata = {"feature_flags": ["beta"], "max_users": 10}
        license_obj = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="active",
            license_metadata=metadata,
        )

        result = license_obj.to_dict()
        assert result["metadata"] == metadata

        # Round-trip: re-fetch from DB and verify metadata persisted
        fetched = db_manager.get_license(UUID(str(license_obj.id)))
        assert fetched.license_metadata == metadata


class TestLazyExpireOnWrite:
    """Tests for expire_stale_licenses + the hardened find_active_license.

    Root cause under test — the RENEWAL DEADLOCK: nothing in the estate ever
    transitions a license out of ``status='active'`` when ``expires_at`` passes,
    so an expired row stays invisible to ``/licenses/active`` (time-filtered)
    while still occupying the ``uq_license_active_tenant_app_subject`` slot AND
    being handed back by ``create_user_license``'s idempotency branch. The
    license becomes permanently unrenewable.
    """

    TENANT = "01000000-0000-7000-8000-000000000001"
    OTHER_TENANT = "01000000-0000-7000-8000-000000000002"

    def _key(self, seed_subject, seed_app, seed_plan):
        return dict(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
        )

    def test_expire_stale_licenses_transitions_expired_active_row(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """An active row whose expires_at is in the past becomes status='expired'."""
        from datetime import UTC, datetime, timedelta

        key = self._key(seed_subject, seed_app, seed_plan)
        lic = db_manager.create_license(
            tenant_id=self.TENANT,
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=400),
            expires_at=datetime.now(UTC) - timedelta(days=21),
            **key,
        )

        transitioned = db_manager.expire_stale_licenses(
            tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
        )

        assert transitioned == 1
        refetched = db_manager.get_license(UUID(str(lic.id)))
        assert refetched.status == "expired"

    def test_expire_stale_licenses_is_idempotent(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """Running the transition twice returns 0 the second time."""
        from datetime import UTC, datetime, timedelta

        key = self._key(seed_subject, seed_app, seed_plan)
        db_manager.create_license(
            tenant_id=self.TENANT,
            status="active",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            **key,
        )

        first = db_manager.expire_stale_licenses(
            tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
        )
        second = db_manager.expire_stale_licenses(
            tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
        )

        assert first == 1
        assert second == 0

    def test_expire_stale_licenses_never_touches_perpetual_or_future(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """THE guard that matters most.

        ``expires_at IS NULL`` means PERPETUAL. Dropping the IS NOT NULL clause
        from the transition predicate would retire every perpetual license in the
        estate — a far worse outage than the bug being fixed. A future-dated
        license must likewise survive untouched.
        """
        from datetime import UTC, datetime, timedelta

        key = self._key(seed_subject, seed_app, seed_plan)

        # Perpetual: expires_at IS NULL. Under tenant A.
        perpetual = db_manager.create_license(
            tenant_id=self.TENANT, status="active", expires_at=None, **key
        )
        # Future-dated: expires_at well ahead of now. Under tenant B (the partial
        # unique index only permits ONE active row per (tenant, app, subject)).
        future = db_manager.create_license(
            tenant_id=self.OTHER_TENANT,
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=365),
            **key,
        )

        assert (
            db_manager.expire_stale_licenses(
                tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
            )
            == 0
        )
        assert (
            db_manager.expire_stale_licenses(
                tenant_id=self.OTHER_TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
            )
            == 0
        )

        assert db_manager.get_license(UUID(str(perpetual.id))).status == "active"
        assert db_manager.get_license(UUID(str(future.id))).status == "active"
        # And both are still reachable through the idempotency lookup.
        assert (
            db_manager.find_active_license(
                tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
            ).id
            == perpetual.id
        )
        assert (
            db_manager.find_active_license(
                tenant_id=self.OTHER_TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
            ).id
            == future.id
        )

    def test_expire_stale_licenses_is_scoped_to_the_provisioned_key(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """A stale row belonging to a DIFFERENT tenant is not collaterally retired."""
        from datetime import UTC, datetime, timedelta

        key = self._key(seed_subject, seed_app, seed_plan)
        mine = db_manager.create_license(
            tenant_id=self.TENANT,
            status="active",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            **key,
        )
        theirs = db_manager.create_license(
            tenant_id=self.OTHER_TENANT,
            status="active",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            **key,
        )

        assert (
            db_manager.expire_stale_licenses(
                tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
            )
            == 1
        )
        assert db_manager.get_license(UUID(str(mine.id))).status == "expired"
        assert db_manager.get_license(UUID(str(theirs.id))).status == "active"

    def test_find_active_license_never_returns_an_expired_row(
        self, db_manager, seed_subject, seed_app, seed_plan, monkeypatch
    ):
        """Belt-and-braces: even if the lazy transition loses a race and does
        nothing, the expires_at window predicate keeps a dead row out of the
        idempotency branch."""
        from datetime import UTC, datetime, timedelta

        from models import DatabaseManager

        key = self._key(seed_subject, seed_app, seed_plan)
        db_manager.create_license(
            tenant_id=self.TENANT,
            status="active",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            **key,
        )

        # Simulate the transition losing the race / doing nothing at all.
        monkeypatch.setattr(
            DatabaseManager, "expire_stale_licenses", lambda self, **kwargs: 0, raising=True
        )

        found = db_manager.find_active_license(
            tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
        )
        assert found is None

    def test_find_active_license_still_returns_a_not_yet_started_license(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        """A PENDING (starts_at in the future) license still occupies the unique
        index slot and is genuinely this key's license, so the idempotency branch
        must keep returning it. Filtering it out would turn re-provisioning into a
        guaranteed IntegrityError."""
        from datetime import UTC, datetime, timedelta

        key = self._key(seed_subject, seed_app, seed_plan)
        pending = db_manager.create_license(
            tenant_id=self.TENANT,
            status="active",
            starts_at=datetime.now(UTC) + timedelta(days=7),
            expires_at=datetime.now(UTC) + timedelta(days=90),
            **key,
        )

        found = db_manager.find_active_license(
            tenant_id=self.TENANT, app_id=key["app_id"], subject_id=key["subject_id"]
        )
        assert found is not None
        assert found.id == pending.id


def _legacy_is_active(lic) -> bool:
    """VERBATIM copy of License.is_active() as it stood before effective_status()
    was extracted. The behavioural baseline — token introspection returns
    ``license_inactive`` off is_active(), so any drift here silently re-authorizes
    or de-authorizes every caller on the platform auth path."""
    from datetime import UTC, datetime

    if lic.status != "active":
        return False
    if lic.starts_at and lic.starts_at > datetime.now(UTC):
        return False
    if lic.expires_at and lic.expires_at < datetime.now(UTC):
        return False
    return True


# (case_id, stored status, starts_at delta days or None, expires_at delta days or
#  None, expected effective_status). Deltas resolved at run time.
_EFFECTIVE_STATUS_MATRIX = [
    ("active_within_window", "active", -1, 30, "active"),
    ("active_perpetual", "active", -1, None, "active"),
    ("active_no_starts_at", "active", None, None, "active"),
    ("active_but_timed_out", "active", -400, -1, "expired"),
    ("active_but_not_yet_started", "active", 7, 90, "pending"),
    ("active_starts_future_and_already_expired", "active", 7, -1, "pending"),
    ("stored_revoked", "revoked", -10, 30, "revoked"),
    ("stored_revoked_perpetual", "revoked", -10, None, "revoked"),
    ("stored_expired", "expired", -400, -1, "expired"),
    ("stored_pending", "pending", -1, 30, "pending"),
    ("stored_unknown_status", "quarantined", -1, 30, "quarantined"),
]


class TestEffectiveStatus:
    """License.effective_status() and its is_active() equivalence."""

    @pytest.mark.parametrize(
        "case_id,status,starts_days,expires_days,expected",
        [pytest.param(*case, id=case[0]) for case in _EFFECTIVE_STATUS_MATRIX],
    )
    def test_effective_status_and_is_active_agree_with_legacy(
        self, case_id, status, starts_days, expires_days, expected
    ):
        """Across the FULL state matrix:

        1. ``effective_status()`` returns the documented label;
        2. ``is_active() == (effective_status() == "active")``;
        3. ``is_active()`` matches the pre-refactor hand-rolled implementation
           EXACTLY — the equivalence in (2) is true by construction, so (3) is
           what actually proves nothing drifted.
        """
        from datetime import UTC, datetime, timedelta

        from models import License

        now = datetime.now(UTC)
        # A real (unflushed) License instance, not a SimpleNamespace stand-in:
        # is_active() now dispatches to effective_status(), so the subject under
        # test must actually be a License.
        lic = License(
            status=status,
            starts_at=None if starts_days is None else now + timedelta(days=starts_days),
            expires_at=None if expires_days is None else now + timedelta(days=expires_days),
        )

        effective = lic.effective_status()
        assert effective == expected

        is_active = lic.is_active()
        assert is_active == (effective == "active")
        assert is_active == _legacy_is_active(lic)

    def test_effective_status_on_a_real_row(self, db_manager, seed_subject, seed_app, seed_plan):
        """Same behaviour through SQLAlchemy instrumentation, not just a namespace
        stand-in (``status`` is a Column-typed attribute, not a plain str)."""
        from datetime import UTC, datetime, timedelta

        lic = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=400),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert lic.effective_status() == "expired"
        assert lic.is_active() is False


class TestTokenModel:
    """Tests for Token model."""

    def test_token_creation(self, seed_token):
        """Test creating a token."""
        token, raw_token = seed_token

        assert token.id is not None
        assert token.token_hash is not None
        assert token.token_type == "api"
        assert token.revoked is False
        assert len(raw_token) > 0

    def test_token_to_dict(self, seed_token):
        """Test Token.to_dict() method."""
        token, _ = seed_token
        result = token.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == str(token.id)
        assert result["license_id"] == str(token.license_id)
        assert result["token_type"] == token.token_type
        assert result["revoked"] == token.revoked
        assert "created_at" in result

    def test_token_is_valid_not_revoked(self, seed_token):
        """Test is_valid() returns True for a non-revoked DB token."""
        token, _ = seed_token
        assert token.is_valid() is True

    def test_token_is_valid_false_revoked(self, db_manager, seed_license):
        """Test is_valid() returns False after revoking a DB-created token."""
        token, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
            expires_at=None,
        )
        assert token.is_valid() is True

        db_manager.revoke_token(raw_token)

        # Re-fetch from DB to get updated state
        token_hash = Token.hash_token(raw_token)
        revoked_token = db_manager.get_token_by_hash(token_hash)
        assert revoked_token.is_valid() is False

    def test_token_introspect_invalid(self, db_manager):
        """Test introspection of invalid token."""
        result = db_manager.introspect_token("invalid-token")
        assert result["active"] is False
        assert result["reason"] == "token_not_found"

    def test_token_revoke(self, db_manager, seed_license):
        """Test revoking a token with full re-fetch verification."""
        # Create token without expiry to avoid timezone issues
        token, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
            expires_at=None,  # No expiry
        )
        success = db_manager.revoke_token(raw_token)
        assert success is True

        # Re-fetch and verify revoked state
        token_hash = Token.hash_token(raw_token)
        refetched = db_manager.get_token_by_hash(token_hash)
        assert refetched.revoked is True
        assert refetched.revoked_at is not None
        assert refetched.is_valid() is False

    def test_token_get_by_hash(self, db_manager, seed_license):
        """Test getting token by hash."""
        token, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
            expires_at=None,
        )
        token_hash = Token.hash_token(raw_token)
        found = db_manager.get_token_by_hash(token_hash)
        assert found is not None
        assert found.id == token.id

    def test_regenerate_token_for_license_happy_path(self, db_manager, seed_license):
        """Atomic rotation revokes every prior live token and mints exactly one new
        VALID token in a single commit."""
        lic_id = UUID(str(seed_license.id))
        old_token, _old_raw = db_manager.create_token(lic_id, expires_at=None)

        revoked_count, new_token, new_raw = db_manager.regenerate_token_for_license(lic_id)

        assert revoked_count == 1
        assert new_token.id != old_token.id
        assert new_token.revoked is False
        assert len(new_raw) > 0

        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == lic_id).all()
            by_id = {t.id: t for t in tokens}
            assert by_id[old_token.id].revoked is True  # prior token revoked
            assert by_id[new_token.id].revoked is False  # replacement live
            live = [t for t in tokens if not t.revoked]
            assert len(live) == 1  # exactly one usable token remains
        finally:
            session.close()

    def test_rotation_inherits_expiry_and_never_lengthens(self, db_manager, seed_license):
        """A rotation must NEVER lengthen a credential's life.

        RED on main: ``regenerate_token_for_license`` took ``expires_at=None`` and
        the route never passed one, so EVERY rotation minted a PERPETUAL
        replacement. Pressing "regenerate" on a 1-day token silently converted it
        into a never-expiring bearer credential. On the mint route perpetuity is
        at least an EXPLICIT choice (omit ``expires_days``); rotation made it the
        silent default for every token it touched.
        """
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        original_expiry = datetime.now(UTC) + timedelta(days=1)
        db_manager.create_token(lic_id, expires_at=original_expiry)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is not None, "rotation minted a PERPETUAL replacement"
        assert new_token.expires_at <= original_expiry, "rotation lengthened the credential"
        assert abs((new_token.expires_at - original_expiry).total_seconds()) < 1

    def test_rotation_keeps_a_perpetual_token_perpetual(self, db_manager, seed_license):
        """A credential that was legitimately perpetual stays perpetual — a
        rotation must not impose an expiry that never existed."""
        lic_id = UUID(str(seed_license.id))
        insert_legacy_perpetual_token(db_manager, lic_id)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is None

    def test_rotation_of_mixed_tokens_keeps_perpetual(self, db_manager, seed_license):
        """One perpetual live token among several means the license already had a
        perpetual credential, so the single replacement stays perpetual."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=datetime.now(UTC) + timedelta(days=1))
        insert_legacy_perpetual_token(db_manager, lic_id)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is None

    def test_rotation_of_several_dated_tokens_inherits_the_latest(self, db_manager, seed_license):
        """Rotation collapses N live credentials into one, so the replacement
        covers the longest-lived thing it destroys — and no longer."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        soon = datetime.now(UTC) + timedelta(days=1)
        later = datetime.now(UTC) + timedelta(days=10)
        db_manager.create_token(lic_id, expires_at=soon)
        db_manager.create_token(lic_id, expires_at=later)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is not None
        assert abs((new_token.expires_at - later).total_seconds()) < 1

    def test_rotation_ignores_the_expiry_of_an_already_revoked_token(
        self, db_manager, seed_license
    ):
        """Only LIVE tokens contribute the inherited expiry.

        A revoked token is not a credential the license still holds, so its
        expiry is not a lifetime worth preserving. Inheriting it would LENGTHEN
        the surviving credential — a 10-year token revoked yesterday would hand
        its 10 years to today's 1-day replacement, which is the very thing this
        fix exists to prevent.
        """
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        soon = datetime.now(UTC) + timedelta(days=1)
        _stale, raw_stale = db_manager.create_token(
            lic_id, expires_at=datetime.now(UTC) + timedelta(days=3650)
        )
        db_manager.revoke_token(raw_stale)
        db_manager.create_token(lic_id, expires_at=soon)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is not None
        assert abs((new_token.expires_at - soon).total_seconds()) < 1

    def test_rotation_ignores_a_revoked_perpetual_token(self, db_manager, seed_license):
        """The severe form of the above: a revoked PERPETUAL token must not make
        every future rotation perpetual.

        ``expires_at IS NULL`` short-circuits the inheritance rule, so counting a
        revoked one would permanently resurrect the defect for that license —
        every subsequent rotation would mint a never-expiring credential, and the
        license could never be brought back under an expiry.
        """
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        soon = datetime.now(UTC) + timedelta(days=1)
        _stale, raw_stale = db_manager.create_token(lic_id, expires_at=None)
        db_manager.revoke_token(raw_stale)
        db_manager.create_token(lic_id, expires_at=soon)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is not None, "a REVOKED perpetual token was inherited"
        assert abs((new_token.expires_at - soon).total_seconds()) < 1

    def test_rotation_with_no_live_tokens_mints_the_new_token_default(
        self, db_manager, seed_license
    ):
        """Documented fallback: nothing replaced means no prior expiry policy to
        preserve, so this is a NEW mint and gets the new-token policy (licenses#244):
        an api token is bounded to TOKEN_API_DEFAULT_EXPIRY_DAYS, not perpetual."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))

        revoked_count, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert revoked_count == 0
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((new_token.expires_at - expected).total_seconds()) < 120

    def test_rotation_does_not_accept_an_expires_at_argument(self, db_manager, seed_license):
        """ "Rotate AND extend" is deliberately not expressible: the parameter was
        REMOVED, not merely defaulted, so no caller can pass a longer expiry."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=datetime.now(UTC) + timedelta(days=1))

        with pytest.raises(TypeError):
            db_manager.regenerate_token_for_license(
                lic_id, expires_at=datetime.now(UTC) + timedelta(days=3650)
            )

    def test_regenerate_token_for_license_is_atomic_on_failure(
        self, db_manager, seed_license, monkeypatch
    ):
        """A crash AFTER the bulk-revoke but BEFORE the new-token commit must roll
        BOTH statements back: the license keeps its existing live token, so it is
        never left with zero tokens.

        This is the regression for the old two-commit rotation: it committed the
        revoke, then separately committed the new token, so a failure in between
        stranded the license with no valid token — and perpetual license tokens
        (expires_at = NULL) are never auto-replaced, making that a PERMANENT
        token_not_found rather than a transient blip.
        """
        lic_id = UUID(str(seed_license.id))
        original_token, original_raw = db_manager.create_token(lic_id, expires_at=None)

        # Fail the mint step (runs after the in-transaction bulk-revoke).
        def _boom() -> str:
            raise RuntimeError("simulated crash mid-rotation")

        monkeypatch.setattr(Token, "generate_token", staticmethod(_boom))

        with pytest.raises(RuntimeError, match="simulated crash mid-rotation"):
            db_manager.regenerate_token_for_license(lic_id)

        # The pre-existing token survives, still live (revoke rolled back), and no
        # partial replacement was persisted.
        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == lic_id).all()
            assert len(tokens) == 1
            assert tokens[0].id == original_token.id
            assert tokens[0].revoked is False
            assert tokens[0].revoked_at is None
        finally:
            session.close()

        # And the original token still introspects/validates as usable.
        refetched = db_manager.get_token_by_hash(Token.hash_token(original_raw))
        assert refetched is not None
        assert refetched.is_valid() is True


class TestRotationNeverMintsAnAlreadyDeadToken:
    """A rotation must never SHORTEN a credential below ``now`` either.

    The 2026-08-08 change closed the upper bound — a rotation can no longer
    LENGTHEN a token's life — and the whole of ``_inherited_rotation_expiry``'s
    reasoning is about that direction. The lower bound was never considered.

    ``regenerate_token_for_license`` selected the expiries to inherit with ONE
    predicate, ``Token.revoked.is_(False)``, and then took ``max()``. "Live" in
    that query means *unrevoked*, not *unexpired* — and nothing in this service
    ever revokes a token when it expires (there is no sweeper), so
    expired-but-unrevoked rows accumulate indefinitely. For a license whose every
    unrevoked token has already expired, ``max()`` therefore returned a timestamp
    in the PAST, and the replacement was dead the moment it was minted:
    ``/license/introspect`` answers ``token_invalid`` on first use.

    That is strictly worse than doing nothing, because the same transaction
    bulk-revokes the tokens it replaces and only the SHA-256 hash is stored — the
    raw values are shown once and are unrecoverable. The caller ends with less
    than it started with and a credential that looks like a service bug.

    Measured in production before this was written: 2 licenses were already in
    that state, one of them ``active`` with ``expires_at IS NULL`` — i.e. the
    route's own 409 gate (which checks the LICENSE, not its tokens) passes and
    the rotation proceeds.

    THE OBVIOUS FIX IS WRONG, which is why the refusal is asserted rather than a
    corrected timestamp: simply adding an expiry predicate to the SELECT turns
    "every live token expired" into an EMPTY inherited set, and the empty-set arm
    is documented to mean perpetual. That silently re-creates the exact defect
    the 2026-08-08 change closed — rotation laundering a short-lived credential
    into a permanent one. The two emptinesses are different and must stay
    distinguishable: *no tokens at all* (nothing to preserve -> perpetual) versus
    *tokens that have all expired* (a policy exists and it says "dead" -> refuse).
    """

    @staticmethod
    def _expired(days: int = 1):
        from datetime import UTC, datetime, timedelta

        return datetime.now(UTC) - timedelta(days=days)

    def test_rotation_refuses_when_every_live_token_has_expired(self, db_manager, seed_license):
        """RED on main: returns a token stamped in the past instead of refusing."""
        from models import AllTokensExpiredError

        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=self._expired())

        with pytest.raises(AllTokensExpiredError):
            db_manager.regenerate_token_for_license(lic_id)

    def test_refusal_destroys_nothing(self, db_manager, seed_license):
        """The refusal leaves every row exactly as it found them.

        A rotation that refuses but has already revoked the old tokens leaves the
        license with zero credentials — the failure mode the atomic-rotation work
        exists to prevent.

        ⚠️ WHAT THIS TEST DOES AND DOES NOT PROVE. It asserts the END STATE, and
        the end state is protected by TWO independent mechanisms: the guard runs
        before the bulk-revoke UPDATE, and ``except Exception: session.rollback()``
        would undo the UPDATE even if it did not. So this test passes against a
        version with the guard moved AFTER the UPDATE — measured, a mutant doing
        exactly that survived all 17 tests in this change. The ordering is pinned
        separately by ``test_the_refusal_issues_no_write_at_all``; this one is
        deliberately the end-state half, and its name says "destroys nothing",
        not "checks first".
        """
        from models import AllTokensExpiredError

        lic_id = UUID(str(seed_license.id))
        old_token, old_raw = db_manager.create_token(lic_id, expires_at=self._expired())

        with pytest.raises(AllTokensExpiredError):
            db_manager.regenerate_token_for_license(lic_id)

        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == lic_id).all()
            assert len(tokens) == 1, "a replacement token was minted despite the refusal"
            assert tokens[0].id == old_token.id
            assert tokens[0].revoked is False, "the refusal still revoked the prior token"
            assert tokens[0].revoked_at is None
        finally:
            session.close()

        assert db_manager.get_token_by_hash(Token.hash_token(old_raw)) is not None

    def test_the_refusal_issues_no_write_at_all(self, db_manager, seed_license):
        """CHECK BEFORE DESTROY, asserted as ordering rather than as end state.

        The sibling test above cannot tell "never wrote" from "wrote and rolled
        back" — both leave identical rows. Relying on the rollback would be a real
        weakening: it makes correctness depend on an ``except`` arm several lines
        away, and it burns a write + a rollback on every refusal. So this listens
        to the statements the engine actually issues and requires the destructive
        UPDATE never to be emitted.

        Same listener idiom as ``tests/unit/test_no_sql_on_event_loop.py`` and
        ``test_introspect_query_count.py``.
        """
        from sqlalchemy import event

        from models import AllTokensExpiredError

        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=self._expired())

        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db_manager.engine, "before_cursor_execute", _record)
        try:
            with pytest.raises(AllTokensExpiredError):
                db_manager.regenerate_token_for_license(lic_id)
        finally:
            event.remove(db_manager.engine, "before_cursor_execute", _record)

        # VACUITY FLOOR: the listener has to have seen the read, or "no UPDATE"
        # is trivially true because nothing was captured at all.
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert selects, f"no SELECT was captured, so nothing was measured: {statements}"

        writes = [
            s for s in statements if s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
        ]
        assert writes == [], (
            "the refusal issued a write before raising. It survives today only "
            f"because the except-arm rolls back — do not rely on that: {writes}"
        )

    def test_refusal_does_not_mint_a_perpetual_token(self, db_manager, seed_license):
        """The naive-filter regression, pinned by NAME.

        If a future change filters expired tokens out of the inherited set without
        distinguishing "filtered to empty" from "was empty", this license gets a
        PERPETUAL replacement — an unbounded credential minted from an expired
        one, which is the 2026-08-08 laundering defect arriving through the other
        door.

        The ONLY load-bearing assertion here is ``pytest.raises``: a perpetual
        mint returns normally. The row inspection that used to follow was dead
        weight — it could only execute once the raise had already been proven, so
        it could never fail — and has been dropped rather than left to read as
        evidence.
        """
        from models import AllTokensExpiredError

        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=self._expired(days=400))

        with pytest.raises(AllTokensExpiredError):
            db_manager.regenerate_token_for_license(lic_id)

    def test_an_expired_token_alongside_a_live_one_is_not_inherited(self, db_manager, seed_license):
        """A single live token is enough: rotate, and inherit ITS expiry.

        ``max()`` happens to give the right answer here even on main (an expired
        timestamp is by definition smaller than an unexpired one), so this is a
        regression guard for the filter, not a RED case. It is the non-vacuity
        control for the three tests above: it proves the refusal is keyed on
        "every live token expired", not on "any expired token present".
        """
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        still_good = datetime.now(UTC) + timedelta(days=7)
        db_manager.create_token(lic_id, expires_at=self._expired())
        db_manager.create_token(lic_id, expires_at=still_good)

        revoked_count, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert revoked_count == 2
        assert new_token.expires_at is not None
        assert abs((new_token.expires_at - still_good).total_seconds()) < 1

    def test_an_expired_token_alongside_a_perpetual_one_stays_perpetual(
        self, db_manager, seed_license
    ):
        """A perpetual live token still wins — the expired one is simply not a
        credential the license holds any more, so it neither blocks the rotation
        nor caps it."""
        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=self._expired())
        insert_legacy_perpetual_token(db_manager, lic_id)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is None

    def test_a_token_expiring_imminently_still_rotates(self, db_manager, seed_license):
        """Boundary: ``> now``, not ``>= now`` — a token with seconds left is still
        a live credential, and refusing to rotate it would be a regression on the
        route's whole purpose."""
        from datetime import UTC, datetime, timedelta

        lic_id = UUID(str(seed_license.id))
        imminent = datetime.now(UTC) + timedelta(seconds=30)
        db_manager.create_token(lic_id, expires_at=imminent)

        _revoked, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert new_token.expires_at is not None
        assert abs((new_token.expires_at - imminent).total_seconds()) < 1

    def test_an_expired_token_does_not_block_a_license_with_no_other_tokens_from_the_no_token_arm(
        self, db_manager, seed_license
    ):
        """The two emptinesses stay distinguishable.

        A license with NO tokens at all keeps the documented fresh-mint fallback
        (a NEW mint: the 365-day api default since licenses#244). This is the arm
        the refusal must not swallow — it is asserted here next to the refusal
        tests so that a change collapsing them fails one of the two.
        """
        lic_id = UUID(str(seed_license.id))

        revoked_count, new_token, _raw = db_manager.regenerate_token_for_license(lic_id)

        assert revoked_count == 0
        assert new_token.expires_at is not None

    def test_an_expired_and_revoked_token_now_refuses_instead_of_minting_perpetual(
        self, db_manager, seed_license
    ):
        """CHANGED DELIBERATELY. This test used to assert the defect it was protecting against.

        It was written to pin a real and still-correct property — a REVOKED token's expiry
        must not be inherited, or a revoked perpetual token would make every future rotation
        perpetual. That half is untouched and still covered.

        But its assertion was ``new_token.expires_at is None``: a license whose only token
        was revoked minted a PERPETUAL replacement. That is the credential-laundering
        ``AllTokensRevokedError`` now closes, and it is reachable through ordinary API use —
        revoke a leaked 30-day token, press regenerate, receive one that never expires. The
        old docstring's fear ("must not turn a no-token license's rotation into a refusal")
        conflated two different states: a license that never HAD a token (fresh-mint
        fallback, still mints perpetually — see
        ``TestTheFreshMintFallbackSurvives::test_a_license_with_no_tokens_still_mints``) and
        a license whose tokens were killed. Only the second refuses.

        Refusing destroys nothing; the way forward is the explicit mint route, where the
        expiry is an argument rather than a silent default.
        """
        lic_id = UUID(str(seed_license.id))
        db_manager.create_token(lic_id, expires_at=self._expired())
        db_manager.revoke_tokens_for_license(lic_id)

        with pytest.raises(AllTokensRevokedError):
            db_manager.regenerate_token_for_license(lic_id)


class TestOriginalIsActiveMethods:
    """Test the ORIGINAL is_active() and is_valid() methods with tz-aware datetimes.

    The conftest monkey-patches these for tz-aware compat, but the originals
    also need coverage for the production PostgreSQL path.

    Subjects are real (unflushed) ORM instances, not SimpleNamespace stand-ins.
    They used to be the latter "to avoid SQLAlchemy instrumentation issues", but
    that made these tests exercise something production never runs: is_active()
    now delegates to effective_status(), which a duck-typed stand-in does not
    have. A stand-in cannot cover "the production PostgreSQL path" by
    construction — the two production callers (main.regenerate_token and
    DatabaseManager.introspect_token) both hold a real License.
    """

    def test_original_license_is_active_true(self):
        """Original is_active returns True for active license with tz-aware dates."""
        from datetime import UTC, datetime, timedelta

        from models import License
        from tests.conftest import _original_license_is_active

        lic = License(
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        assert _original_license_is_active(lic) is True

    def test_original_license_is_active_false_expired(self):
        """Original is_active returns False for expired license."""
        from datetime import UTC, datetime, timedelta

        from models import License
        from tests.conftest import _original_license_is_active

        lic = License(
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=30),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert _original_license_is_active(lic) is False

    def test_original_license_is_active_false_future_start(self):
        """Original is_active returns False when starts_at is in the future."""
        from datetime import UTC, datetime, timedelta

        from models import License
        from tests.conftest import _original_license_is_active

        lic = License(
            status="active",
            starts_at=datetime.now(UTC) + timedelta(days=10),
            expires_at=datetime.now(UTC) + timedelta(days=40),
        )
        assert _original_license_is_active(lic) is False

    def test_original_license_is_active_false_revoked(self):
        """Original is_active returns False for non-active status."""
        from models import License
        from tests.conftest import _original_license_is_active

        lic = License(status="revoked", starts_at=None, expires_at=None)
        assert _original_license_is_active(lic) is False

    def test_original_license_is_active_perpetual(self):
        """Original is_active returns True for perpetual license (no expiry)."""
        from datetime import UTC, datetime, timedelta

        from models import License
        from tests.conftest import _original_license_is_active

        lic = License(
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=None,
        )
        assert _original_license_is_active(lic) is True

    def test_original_token_is_valid_true(self):
        """Original is_valid returns True for non-revoked, non-expired token."""
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from typing import cast

        from models import Token
        from tests.conftest import _original_token_is_valid

        tok = cast(
            Token, SimpleNamespace(revoked=False, expires_at=datetime.now(UTC) + timedelta(days=30))
        )
        assert _original_token_is_valid(tok) is True

    def test_original_token_is_valid_false_expired(self):
        """Original is_valid returns False for expired token."""
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from typing import cast

        from models import Token
        from tests.conftest import _original_token_is_valid

        tok = cast(
            Token, SimpleNamespace(revoked=False, expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        assert _original_token_is_valid(tok) is False

    def test_original_token_is_valid_false_revoked(self):
        """Original is_valid returns False for revoked token."""
        from types import SimpleNamespace
        from typing import cast

        from models import Token
        from tests.conftest import _original_token_is_valid

        tok = cast(Token, SimpleNamespace(revoked=True, expires_at=None))
        assert _original_token_is_valid(tok) is False

    def test_original_token_is_valid_no_expiry(self):
        """Original is_valid returns True for token with no expiry (perpetual)."""
        from types import SimpleNamespace
        from typing import cast

        from models import Token
        from tests.conftest import _original_token_is_valid

        tok = cast(Token, SimpleNamespace(revoked=False, expires_at=None))
        assert _original_token_is_valid(tok) is True


class TestTokenUtilityMethods:
    """Tests for Token static utility methods."""

    def test_hash_token(self):
        """Test Token.hash_token produces consistent SHA-256 hash."""
        raw = "my-test-token"
        hash1 = Token.hash_token(raw)
        hash2 = Token.hash_token(raw)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex

    def test_generate_token(self):
        """Test Token.generate_token produces unique tokens."""
        tokens = {Token.generate_token() for _ in range(100)}
        assert len(tokens) == 100  # All unique


class TestModelRelationships:
    """Tests for model relationships and foreign keys."""

    def test_plan_app_relationship(self, seed_plan, seed_app):
        """Test Plan -> App relationship."""
        assert seed_plan.app_id == seed_app.id

    def test_license_relationships(self, seed_license, seed_subject, seed_app, seed_plan):
        """Test License relationships."""
        assert seed_license.subject_id == seed_subject.id
        assert seed_license.app_id == seed_app.id
        assert seed_license.plan_id == seed_plan.id

    def test_token_license_relationship(self, seed_token, seed_license):
        """Test Token -> License relationship."""
        token, _ = seed_token
        assert token.license_id == seed_license.id

    def test_plan_requires_valid_app_id(self, db_manager):
        """Test that creating a plan with a non-existent app_id raises IntegrityError."""
        with pytest.raises(IntegrityError):
            db_manager.create_plan(
                app_id=uuid4(),
                code="orphan",
                name="Orphan Plan",
            )

    def test_license_requires_valid_subject_id(self, db_manager, seed_app, seed_plan):
        """Test that creating a license with a non-existent subject_id raises IntegrityError."""
        with pytest.raises(IntegrityError):
            db_manager.create_license(
                subject_id=uuid4(),
                app_id=UUID(str(seed_app.id)),
                plan_id=UUID(str(seed_plan.id)),
                # Registered tenant so the ONLY FK violation is the bad subject_id.
                tenant_id="01000000-0000-7000-8000-000000000001",
                status="active",
            )

    def test_token_requires_valid_license_id(self, db_manager):
        """Test that creating a token with a non-existent license_id raises IntegrityError."""
        with pytest.raises(IntegrityError):
            db_manager.create_token(
                license_id=uuid4(),
                token_type="api",
            )
