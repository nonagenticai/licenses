"""Smoke test for scripts/seed_portal_n8n_dev.py after the migration-016 retype.

The script used to write the literal string ``"n8n-dev"`` into ``license.tenant_id``.
Since migration 016 that column is a ``uuid`` FK to ``tenants.id``, so the insert
crashed (``invalid input syntax for type uuid: "n8n-dev"`` + FK violation). The
fix resolves the tenant NAME to its registered UUID at run time. This test seeds
the ``n8n-dev`` tenant and runs the seeder end-to-end, asserting it mints
licenses carrying the tenant's canonical UUID (not the string, and no crash).

This script is a manual dev seeder with no CI job, so this test is its only
automated guard.
"""

from __future__ import annotations

import models
import scripts.seed_portal_n8n_dev as seeder
from models import License


def _route_seeder_through_fixture(db_manager, monkeypatch):
    """Make the seeder use the test fixture's DB manager.

    The seeder does ``DatabaseManager(os.getenv("DATABASE_URL"))``, and
    ``DatabaseManager`` defaults ``POSTGRES_SSLMODE=require`` — so against CI's
    ephemeral (no-SSL) postgres its own connection fails with "server does not
    support SSL". Patching ``models.DatabaseManager`` (the seeder does
    ``from models import DatabaseManager`` at call time) routes it through the
    fixture's already-connected manager, so the test is env-independent. The
    DATABASE_URL env just has to be non-empty to pass the seeder's presence check.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/db")
    monkeypatch.setattr(models, "DatabaseManager", lambda *a, **k: db_manager)


def test_seed_resolves_tenant_name_to_uuid(db_manager, monkeypatch):
    # Register the n8n-dev tenant (migration 003 does this in prod).
    tenant = db_manager.create_tenant(name="n8n-dev", owner_key="platform")
    tenant_uuid = str(tenant.id)

    _route_seeder_through_fixture(db_manager, monkeypatch)
    seeder.main()

    session = db_manager.get_session()
    try:
        licenses = session.query(License).all()
        assert licenses, "seeder should have created licenses"
        # Every seeded license must carry the resolved UUID, never the name.
        for lic in licenses:
            assert str(lic.tenant_id) == tenant_uuid
    finally:
        session.close()


def test_seed_fails_loud_when_tenant_unregistered(db_manager, monkeypatch):
    """With no n8n-dev tenant registered, the seeder must exit non-zero rather
    than attempt an FK-violating insert."""
    import pytest

    # db_manager fixture truncates tenants each test; do NOT register n8n-dev.
    _route_seeder_through_fixture(db_manager, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        seeder.main()
    assert exc.value.code == 1
