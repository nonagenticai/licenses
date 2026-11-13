#!/usr/bin/env python3
"""
Seed script for creating perpetual licenses for georgiedekker portal user
with tenant_id="n8n-dev" (matching the n8n custom nodes tenant).

Keycloak account (portal realm):
    sub: e148e8af-3751-49bf-a0fa-56ab46d31ddc
    username: georgiedekker
    email: georgiedekker@gmail.com

Usage:
    cd /path/to/licenses
    source .venv/bin/activate
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@localhost:5432/licenses?sslmode=require" \
      python scripts/seed_portal_n8n_dev.py
"""

import hashlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    App,
    License,
    LongLivedRegistration,
    Plan,
    Subject,
    Token,
    resolve_new_token_expiry,
)

# All 13 modules that need licenses
MODULES = [
    {
        "key": "artifacts",
        "name": "Artifacts Module",
        "description": "Artifact registry for file storage",
    },
    {
        "key": "coding",
        "name": "Coding Module",
        "description": "Code analysis and assistance",
    },
    {"key": "kag", "name": "KAG Module", "description": "Knowledge graph operations"},
    {
        "key": "kanban",
        "name": "Kanban Module",
        "description": "Planka kanban board integration",
    },
    {
        "key": "keycloak",
        "name": "Keycloak Module",
        "description": "Keycloak identity management",
    },
    {"key": "licenses", "name": "Licenses Module", "description": "License management"},
    {
        "key": "mcp",
        "name": "MCP Module",
        "description": "Model Context Protocol integration",
    },
    {
        "key": "procedures",
        "name": "Procedures Module",
        "description": "Procedure/workflow management",
    },
    {
        "key": "requirements",
        "name": "Requirements Module",
        "description": "Requirements tracking",
    },
    {"key": "secrets", "name": "Secrets Module", "description": "Secret management"},
    {"key": "tools", "name": "Tools Module", "description": "Tool registry"},
    {
        "key": "workers",
        "name": "Workers Module",
        "description": "Worker/agent management",
    },
    {
        "key": "skill_builder",
        "name": "Skill Builder Module",
        "description": "Skill building/training",
    },
]

# Portal user configuration
PORTAL_SUBJECT_KEYCLOAK_SUB = "e148e8af-3751-49bf-a0fa-56ab46d31ddc"
PORTAL_SUBJECT_REALM = "portal"
# The canonical tenant NAME (migration 003 seeds it with a random uuidv7 id).
# Since migration 016 retyped license.tenant_id VARCHAR -> uuid (real FK to
# tenants.id), the license can no longer carry the string "n8n-dev" — it must
# carry the tenant's UUID. main() resolves the name to its registered id at run
# time (fail-loud if absent) rather than hardcoding a per-DB random id.
PORTAL_TENANT_NAME = "n8n-dev"  # Match the n8n custom nodes tenant
PORTAL_PLAN_CODE = "portal-n8n-dev"


def generate_token():
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash token using SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.")
        print("Run: export DATABASE_URL='postgresql+psycopg://...'")
        sys.exit(1)
    assert db_url is not None

    from models import DatabaseManager

    db_manager = DatabaseManager(db_url)

    # Resolve the n8n-dev tenant to its canonical UUID. Since migration 016,
    # license.tenant_id is a real FK to tenants.id, so a license MUST reference a
    # registered tenant by its UUID (the literal "n8n-dev" name no longer casts
    # to the uuid column and would violate the FK). Fail loud if the tenant was
    # never registered (migration 003 seeds it; run migrations first).
    tenant = db_manager.get_tenant(PORTAL_TENANT_NAME)
    if tenant is None:
        print(
            f"ERROR: tenant {PORTAL_TENANT_NAME!r} is not registered. "
            "Apply the DB migrations (003 seeds it) before seeding licenses."
        )
        sys.exit(1)
    portal_tenant_id = str(tenant.id)

    # licenses#244: the n8n licence tokens are the REGISTERED long-lived `api` class
    # (n8n_custom_nodes/CLAUDE.md: long-living, never modified or regenerated). The
    # policy refuses this unless the tenant is in LONG_LIVED_API_TENANTS.
    n8n_registration = LongLivedRegistration(
        owner="n8n_custom_nodes (n8n-dev licence-token credentials)",
        reason=(
            "n8n licence tokens are long-living by rule (n8n_custom_nodes/CLAUDE.md "
            "'DO NOT TOUCH LICENSE TOKENS'); minted by seed_portal_n8n_dev.py"
        ),
    )
    n8n_token_expiry = resolve_new_token_expiry(
        "api",
        None,
        n8n_registration,
        tenant_keys=frozenset({str(tenant.id), str(tenant.name)}),
    )

    session = db_manager.get_session()

    now = datetime.now(UTC)

    # Create or get portal subject (georgiedekker)
    subject = session.query(Subject).filter_by(keycloak_sub=PORTAL_SUBJECT_KEYCLOAK_SUB).first()
    if not subject:
        subject = Subject(
            keycloak_sub=PORTAL_SUBJECT_KEYCLOAK_SUB,
            keycloak_realm=PORTAL_SUBJECT_REALM,
        )
        session.add(subject)
        session.flush()
        print(f"Created Subject: {subject.id} (georgiedekker - portal realm)")
    else:
        print(f"Using existing Subject: {subject.id} (georgiedekker)")

    tokens = {}

    for module in MODULES:
        print(f"\n--- {module['key']} ---")

        # 1. Reuse existing App (created by previous seed scripts)
        app = session.query(App).filter_by(key=module["key"]).first()
        if not app:
            app = App(
                key=module["key"],
                name=module["name"],
                description=module["description"],
                revoked=False,
            )
            session.add(app)
            session.flush()
            print(f"  Created App: {app.id}")
        else:
            print(f"  Using existing App: {app.id}")

        # 2. Create or get Plan (perpetual)
        plan = (
            session.query(Plan)
            .filter_by(app_id=app.id, code=PORTAL_PLAN_CODE, region_id=None)
            .first()
        )
        if not plan:
            plan = Plan(
                app_id=app.id,
                code=PORTAL_PLAN_CODE,
                name=f"{module['name']} - Portal n8n-dev (Perpetual)",
                description="Portal user license (n8n-dev tenant) - never expires",
                duration_days=None,
                region_id=None,
                rate_limit_rpm=9999,
                rate_limit_burst=1000,
                quota_monthly=None,
                revoked=False,
            )
            session.add(plan)
            session.flush()
            print(f"  Created Plan: {plan.id}")
        else:
            print(f"  Using existing Plan: {plan.id}")

        # 3. Create or get License (perpetual, tenant_id=n8n-dev)
        license_obj = (
            session.query(License)
            .filter_by(
                subject_id=subject.id,
                app_id=app.id,
                tenant_id=portal_tenant_id,
            )
            .first()
        )
        if not license_obj:
            license_obj = License(
                subject_id=subject.id,
                app_id=app.id,
                plan_id=plan.id,
                tenant_id=portal_tenant_id,
                status="active",
                starts_at=now,
                expires_at=None,
            )
            session.add(license_obj)
            session.flush()
            print(f"  Created License: {license_obj.id}")
        else:
            setattr(license_obj, "status", "active")
            setattr(license_obj, "expires_at", None)
            license_obj.plan_id = plan.id
            print(f"  Updated License: {license_obj.id}")

        # 4. Create Token (registered long-lived, licenses#244)
        raw_token = generate_token()
        token_hash = hash_token(raw_token)

        token = Token(
            license_id=license_obj.id,
            token_hash=token_hash,
            token_type="api",
            expires_at=n8n_token_expiry,
            revoked=False,
            long_lived_owner=n8n_registration.owner,
            long_lived_reason=n8n_registration.reason,
        )
        session.add(token)
        print(f"  Created Token (hash: {token_hash[:16]}...)")

        tokens[module["key"]] = raw_token

    session.commit()
    session.close()

    print("\n" + "=" * 70)
    print("PORTAL USER LICENSE TOKENS (georgiedekker, tenant=n8n-dev)")
    print("=" * 70)
    print(f"\nKeycloak sub: {PORTAL_SUBJECT_KEYCLOAK_SUB}")
    print(f"Tenant:       {PORTAL_TENANT_NAME} ({portal_tenant_id})")
    print(f"Realm:        {PORTAL_SUBJECT_REALM}")
    print("\nTokens:\n")

    for module_key, token in tokens.items():
        print(f"{module_key}: {token}")

    print("\n" + "=" * 70)
    print("JSON Format:")
    print("=" * 70)
    print(json.dumps(tokens, indent=2))

    output_file = "/tmp/n8n_portal_n8n_dev_tokens.json"  # nosec B108
    with open(output_file, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"\nTokens saved to: {output_file}")


if __name__ == "__main__":
    main()
