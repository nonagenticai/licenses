"""Tests for the byo_mode backfill classifier (scripts/backfill_tenants_byo_mode.py).

Locks the 2026-07-07 posture: unlisted tenants default to ``byo_required``, the
owner's internal tenants stay ``local_allowed``, and system/infra stays
``platform_only``. Guards against regressing the render/E2E service-account
tenant back to ``byo_required`` (which hard-fails the tokenless-S2S E2E nightlies
with WM ``402 byo_llm_not_configured``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_tenants_byo_mode.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_backfill_byo_mode_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ⛔ REGISTER BEFORE EXEC. The script under test defines a frozen @dataclass, and
    # `dataclasses._is_type` resolves the decorated class's own module via
    # `sys.modules.get(cls.__module__).__dict__`. A module built by `module_from_spec`
    # is NOT in `sys.modules` until someone puts it there, so that lookup returns None
    # and this file dies at COLLECTION with
    # `AttributeError: 'NoneType' object has no attribute '__dict__'` — which reads as a
    # broken test file rather than a loader mistake.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_module()
classify = _MODULE.classify
_pick_name = _MODULE._pick_name

# The identity both nightly E2E harnesses (w2v_frontend + w2v_orchestrator) bind
# to; runs as the platform's own render/service-account tenant. Re-keyed from
# e148e8af-… by migration 046 — that id is a Keycloak principal's sub, not a tenant.
E2E_RENDER_TENANT = "01a1113b-674f-72b0-a84b-dc746d39afca"


def test_e2e_render_tenant_is_local_allowed():
    # Regression: omitting this from LOCAL_ALLOWED on the 2026-07-07 flip dropped
    # it to byo_required and broke the E2E nightlies (WM 402 byo_llm_not_configured).
    assert classify(E2E_RENDER_TENANT) == "local_allowed"


def test_owner_internal_peer_is_local_allowed():
    assert classify("019de842-c92a-7e37-8fdd-eb0d8c5396f9") == "local_allowed"  # n8n-dev


def test_unlisted_tenant_defaults_to_byo_required():
    # The commercial default must NOT be weakened by this fix.
    assert classify("11111111-2222-3333-4444-555555555555") == "byo_required"


def test_pick_name_never_equals_the_uuid():
    # Root-cause guard: a row whose name == its id::text is exactly what migration
    # 004 deletes. The backfill must NEVER produce such a name, or a re-run would
    # recreate rows that the next 004-style cleanup silently wipes again.
    for tid in (
        "11111111-2222-3333-4444-555555555555",  # unlisted → no friendly name
        E2E_RENDER_TENANT,  # has a friendly name
        "00000000-0000-0000-0000-000000000000",  # system tenant
    ):
        name = _pick_name(tid)
        assert name != tid
        assert name != str(tid)


def test_pick_name_uses_friendly_name_when_known():
    assert _pick_name(E2E_RENDER_TENANT) == "portal-render-e2e"
    assert _pick_name("0195f48b-d000-7000-8000-000000000099") == "fr-test-user"


def test_known_commercial_stays_byo_required():
    assert classify("019edc1e-313d-779d-9330-fe013c705be8") == "byo_required"  # subhankar (kag)


def test_system_tenant_stays_platform_only():
    assert classify("00000000-0000-0000-0000-000000000000") == "platform_only"
