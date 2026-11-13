#!/usr/bin/env python3
"""
Seed script for creating perpetual test licenses for all n8n custom node modules.

Usage:
    cd /path/to/licenses
    source .venv/bin/activate
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@localhost:6432/licenses" python scripts/seed_test_licenses.py

Output:
    Prints tokens in a format ready for n8n credentials.
"""

import hashlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from models import TOKEN_API_DEFAULT_EXPIRY_DAYS, App, License, Plan, Subject, Token

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

# Test configuration
TEST_PLAN_CODE = "test"
TEST_TENANT_ID = "test-tenant"
TEST_SUBJECT_KEYCLOAK_SUB = "test-user-perpetual"
TEST_SUBJECT_REALM = "secure-apps"


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

    # Use DatabaseManager which handles SSL certificates automatically
    from models import DatabaseManager

    db_manager = DatabaseManager(db_url)
    session = db_manager.get_session()

    now = datetime.now(UTC)

    # Create or get test subject
    subject = session.query(Subject).filter_by(keycloak_sub=TEST_SUBJECT_KEYCLOAK_SUB).first()
    if not subject:
        subject = Subject(
            keycloak_sub=TEST_SUBJECT_KEYCLOAK_SUB,
            keycloak_realm=TEST_SUBJECT_REALM,
        )
        session.add(subject)
        session.flush()
        print(f"Created Subject: {subject.id}")
    else:
        print(f"Using existing Subject: {subject.id}")

    tokens = {}

    for module in MODULES:
        print(f"\n--- {module['key']} ---")

        # 1. Create or get App
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

        # 2. Create or get Plan (perpetual - duration_days=None, region_id=None)
        plan = (
            session.query(Plan)
            .filter_by(
                app_id=app.id,
                code=TEST_PLAN_CODE,
                region_id=None,  # Global plan
            )
            .first()
        )
        if not plan:
            plan = Plan(
                app_id=app.id,
                code=TEST_PLAN_CODE,
                name=f"{module['name']} - Test Plan (Perpetual)",
                description="Internal testing - never expires",
                duration_days=None,  # Perpetual
                region_id=None,  # Global
                rate_limit_rpm=9999,
                rate_limit_burst=1000,
                quota_monthly=None,  # Unlimited
                revoked=False,
            )
            session.add(plan)
            session.flush()
            print(f"  Created Plan: {plan.id}")
        else:
            print(f"  Using existing Plan: {plan.id}")

        # 3. Create or get License (perpetual - expires_at=None)
        license_obj = (
            session.query(License)
            .filter_by(
                subject_id=subject.id,
                app_id=app.id,
                tenant_id=TEST_TENANT_ID,
            )
            .first()
        )
        if not license_obj:
            license_obj = License(
                subject_id=subject.id,
                app_id=app.id,
                plan_id=plan.id,
                tenant_id=TEST_TENANT_ID,
                status="active",
                starts_at=now,
                expires_at=None,  # Perpetual
            )
            session.add(license_obj)
            session.flush()
            print(f"  Created License: {license_obj.id}")
        else:
            # Update existing license to active/perpetual
            setattr(license_obj, "status", "active")
            setattr(license_obj, "expires_at", None)
            license_obj.plan_id = plan.id
            print(f"  Updated License: {license_obj.id}")

        # 4. Create Token (bounded, licenses#244)
        raw_token = generate_token()
        token_hash = hash_token(raw_token)

        token = Token(
            license_id=license_obj.id,
            token_hash=token_hash,
            token_type="api",
            # EXPLICIT bounded expiry (licenses#244): no longer perpetual.
            expires_at=datetime.now(UTC) + timedelta(days=TOKEN_API_DEFAULT_EXPIRY_DAYS),
            revoked=False,
        )
        session.add(token)
        print(f"  Created Token (hash: {token_hash[:16]}...)")

        tokens[module["key"]] = raw_token

    session.commit()
    session.close()

    # Output all tokens
    print("\n" + "=" * 70)
    print("PERPETUAL TEST LICENSE TOKENS")
    print("=" * 70)
    print("\nCopy these into your n8n credentials:\n")

    for module_key, token in tokens.items():
        print(f"{module_key}: {token}")

    print("\n" + "=" * 70)
    print("JSON Format:")
    print("=" * 70)
    print(json.dumps(tokens, indent=2))

    # Save to file (operator script — predictable path is intentional for copy-out)
    output_file = "/tmp/n8n_test_tokens.json"  # nosec B108
    with open(output_file, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"\nTokens saved to: {output_file}")


if __name__ == "__main__":
    main()
