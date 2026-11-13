"""Unit tests for the kag tenant provisioning CLI arg parsing.

Focused on the `--plan` flag added to make the plan code configurable: kag has
no `standard` plan (that's fr), so the script must default to `perpetual` and
accept an override. Pure arg-parsing — no DB required.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

# Load scripts/provision_kag_tenant.py by path — `scripts/` is not a package.
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "provision_kag_tenant.py",
)
_spec = importlib.util.spec_from_file_location("provision_kag_tenant", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
provision_kag_tenant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_kag_tenant)

_BASE_ARGS = [
    "--tenant-id",
    "0195f48b-d000-7000-8000-000000000001",
    "--tenant-name",
    "acme-corp",
    "--owner-sub",
    "owner-keycloak-sub",
    "--tenant-owner",
    "platform",
]


def test_plan_defaults_to_perpetual() -> None:
    args = provision_kag_tenant._parse_args(_BASE_ARGS)
    assert args.plan == "perpetual"
    assert provision_kag_tenant.DEFAULT_KAG_PLAN_CODE == "perpetual"


def test_plan_is_overridable() -> None:
    args = provision_kag_tenant._parse_args([*_BASE_ARGS, "--plan", "portal-test"])
    assert args.plan == "portal-test"


def test_required_args_still_enforced() -> None:
    with pytest.raises(SystemExit):
        provision_kag_tenant._parse_args(["--owner-sub", "x"])  # missing --tenant-id


def test_tenant_name_is_required() -> None:
    """--tenant-name is required so a license is only ever minted for a tenant we
    also register in the tenants table (the license.tenant_id -> tenants.id FK
    invariant)."""
    with pytest.raises(SystemExit):
        provision_kag_tenant._parse_args(
            [
                "--tenant-id",
                "0195f48b-d000-7000-8000-000000000001",
                "--owner-sub",
                "owner-keycloak-sub",
            ]  # missing --tenant-name
        )


def test_tenant_name_parsed() -> None:
    args = provision_kag_tenant._parse_args(_BASE_ARGS)
    assert args.tenant_name == "acme-corp"


def test_realm_and_expiry_defaults_unchanged() -> None:
    args = provision_kag_tenant._parse_args(_BASE_ARGS)
    assert args.realm == "portal"
    assert args.expires_at is None
