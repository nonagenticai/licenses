"""An invalid ``X-Tenant-Id`` must not be echoed back when it is a credential.

THE DEFECT
----------
``auth/tenant.py`` returned HTTP 400 with a detail f-string containing the raw
header value, and logged the same value at WARNING. Its own comment defended
this: "it IS the diagnosis, it is not a secret (a tenant_id is a public
identifier the gateway injects)".

That premise was true when written and is false now. The fleet's n8n transport
convention deliberately carries the licence token IN THAT HEADER --
``X-Tenant-Id: Bearer <licenceToken>`` -- so the single most likely malformed
value on this exact path is a LIVE CREDENTIAL. The endpoint therefore reflected
a working credential into a response body and into logs, where an unauthenticated
caller could read it back simply by sending it.

The fix keeps the diagnosis and drops the secret. Nothing about the useful case
changes: a legacy text label like 'n8n-dev' -- the exact thing the message exists
to explain -- is still quoted in full.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from auth.tenant import _safe_tenant_value, validate_tenant_uuid

# Shape-accurate stand-ins. Neither is a real credential.
FAKE_LICENCE_TOKEN = "Bearer " + "aB3-x" * 8  # pragma: allowlist secret
FAKE_BARE_TOKEN = "Kx9_" + "Qm4Z" * 9  # pragma: allowlist secret


class TestCredentialsAreNeverEchoed:
    @pytest.mark.parametrize("value", [FAKE_LICENCE_TOKEN, FAKE_BARE_TOKEN])
    def test_the_400_body_does_not_contain_the_value(self, value):
        with pytest.raises(HTTPException) as excinfo:
            validate_tenant_uuid(value, source="X-Tenant-Id")

        assert excinfo.value.status_code == 400
        detail = str(excinfo.value.detail)
        assert value not in detail
        # ...and neither does the secret half of a schemed credential.
        assert value.removeprefix("Bearer ") not in detail

    def test_a_bearer_value_is_named_as_a_credential(self):
        """The operator still learns the actual diagnosis: you sent a token."""
        rendered = _safe_tenant_value(FAKE_LICENCE_TOKEN)
        assert "bearer credential redacted" in rendered
        assert "aB3-x" not in rendered

    def test_an_opaque_value_still_gets_a_stable_correlator(self):
        """Redaction must not destroy the ability to correlate across log lines."""
        first = _safe_tenant_value(FAKE_BARE_TOKEN)
        second = _safe_tenant_value(FAKE_BARE_TOKEN)
        assert first == second
        assert "sha256:" in first
        assert FAKE_BARE_TOKEN not in first
        assert _safe_tenant_value(FAKE_BARE_TOKEN + "x") != first


class TestTheUsefulDiagnosisSurvives:
    @pytest.mark.parametrize("label", ["n8n-dev", "portal-render-e2e", "fr-test-user", "ahtapol"])
    def test_legacy_text_labels_are_still_quoted_in_full(self, label):
        """These are public tenant NAMES; showing them is the whole point."""
        with pytest.raises(HTTPException) as excinfo:
            validate_tenant_uuid(label, source="X-Tenant-Id")

        detail = str(excinfo.value.detail)
        assert label in detail
        assert "tenant_name" in detail

    def test_a_valid_uuid_is_unaffected(self):
        assert (
            validate_tenant_uuid("0195f48b-d000-7000-8000-000000000099", source="X-Tenant-Id")
            == "0195f48b-d000-7000-8000-000000000099"
        )

    def test_empty_still_401s_not_400s(self):
        with pytest.raises(HTTPException) as excinfo:
            validate_tenant_uuid("", source="X-Tenant-Id")
        assert excinfo.value.status_code == 401
