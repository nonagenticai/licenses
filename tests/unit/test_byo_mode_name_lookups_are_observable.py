"""The auth-exempt byo-mode lookup is UUID-only; a tokenless NAME lookup is refused and logged.

``GET /tenants/{name_or_id}/byo-mode`` is deliberately tokenless for UUID selectors —
``worker_management`` and ``billing-service`` call it with no license token to enforce
``byo_required`` on S2S worker runs, and those callers FAIL OPEN on any non-200, so a
401 there would silently disable enforcement fleet-wide rather than break anything
visibly.

The exemption used to cover NAME selectors too, which made it a name-to-UUID oracle for
anyone inside the pod CIDR. It was first instrumented (2026-08-18, PR #156) rather than
narrowed, because narrowing against an unknown caller that passes a name would fail
open invisibly. The measurement came back clean — both callers resolve a UUID before
calling, 23 of 23 live requests were UUIDs, the warning never fired — so the exemption
is now narrowed: a name selector takes the normal auth path (401 without a bearer,
unchanged with one).

These tests pin both halves — the UUID path must stay open AND silent, or the S2S
callers fail open; the name path must be refused AND logged, or the oracle is back.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("seed_app")

_MARKER = "byo_mode_open_route_name_lookup"


class TestANameShapedLookupIsLogged:
    def test_a_name_selector_is_recorded(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            response = client.get("/tenants/acme-corp/byo-mode")
        # The name selector is no longer exempt: tokenless, it is refused by the
        # normal auth path for the protected /tenants prefix — AND the refusal is
        # logged, because no caller in the tree does this and a 401 to a fail-open
        # S2S caller is invisible everywhere else.
        assert response.status_code == 401
        assert any(_MARKER in r.getMessage() for r in caplog.records), (
            f"no name-lookup warning was emitted; records={[r.getMessage() for r in caplog.records]}"
        )

    def test_the_selector_is_in_the_message(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A count alone cannot answer 'which caller, looking up what'."""
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            client.get("/tenants/acme-corp/byo-mode")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "acme-corp" in joined

    def test_the_per_app_shape_is_no_longer_exempt_at_all(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The per-app shape left the exemption entirely (2026-08-29).

        It used to be the second alternation in ``_BYO_MODE_OPEN_ROUTES`` and this
        test asserted it emitted the oracle warning. It no longer can: the shape
        is not exempt, so an unauthenticated GET is refused by the auth
        middleware before any selector is inspected.

        Asserting the 401 as well as the silence matters. Silence alone is what a
        DELETED route looks like too, and this route now exists — so a test that
        only checked for no-warning would pass just as happily if the exemption
        came back and the route started answering anonymously.
        """
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            response = client.get("/tenants/acme-corp/apps/coding/byo-mode")
        assert response.status_code == 401
        assert not [r for r in caplog.records if _MARKER in r.getMessage()]


class TestAUuidLookupStaysSilent:
    """The discriminating half. A warning on every request is not a signal."""

    def test_a_uuid_selector_logs_nothing(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            client.get("/tenants/0195f48b-d000-7000-8000-000000000001/byo-mode")
        assert not [r for r in caplog.records if _MARKER in r.getMessage()]

    def test_an_uppercase_uuid_also_stays_silent(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """UUIDs are case-insensitive on the wire; a case-sensitive shape test would
        report every upper-case caller as an oracle probe and bury the real one."""
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            client.get("/tenants/0195F48B-D000-7000-8000-000000000001/byo-mode")
        assert not [r for r in caplog.records if _MARKER in r.getMessage()]

    def test_a_uuid_on_the_per_app_shape_stays_silent(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Still silent, but for a different reason now — so it asserts the reason.

        This used to prove "a UUID selector on the exempt per-app shape does not
        trip the name-oracle warning". The per-app shape is no longer exempt, so
        the request never reaches the selector check at all. Without the status
        assertion this case would be vacuous: it would pass against a route that
        does not exist, and against one that answers anonymously in silence.
        """
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            response = client.get(
                "/tenants/0195f48b-d000-7000-8000-000000000001/apps/coding/byo-mode"
            )
        assert response.status_code == 401
        assert not [r for r in caplog.records if _MARKER in r.getMessage()]


class TestTheExemptionIsUuidOnly:
    """The auth boundary, stated from both sides of the selector shape."""

    def test_a_uuid_lookup_is_still_reachable_without_a_token(self, client: TestClient) -> None:
        """The S2S consumers. A 401 here is the silent fleet-wide fail-open."""
        assert (
            client.get("/tenants/0195f48b-d000-7000-8000-000000000001/byo-mode").status_code != 401
        )

    def test_a_name_lookup_without_a_token_is_401(self, client: TestClient) -> None:
        """The oracle, closed. A tokenless name lookup is refused before the
        route can resolve the name to a UUID."""
        assert client.get("/tenants/acme-corp/byo-mode").status_code == 401

    def test_a_name_lookup_with_a_valid_token_is_unchanged(
        self, auth_client_admin: TestClient
    ) -> None:
        """Only the tokenless path narrowed. ``seed-tenant-primary`` is the seed
        tenant's name (conftest ``_truncate_all``); an unknown name keeps the
        route's own 404 rather than collapsing into the auth 401."""
        found = auth_client_admin.get("/tenants/seed-tenant-primary/byo-mode")
        assert found.status_code == 200
        assert found.json()["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert auth_client_admin.get("/tenants/acme-corp/byo-mode").status_code == 404

    def test_the_write_variant_is_still_protected(self, client: TestClient) -> None:
        """PATCH keeps its ``license-admin`` gate — only GET is exempt."""
        response = client.patch("/tenants/acme-corp/byo-mode", json={"byo_mode": "byo_required"})
        assert response.status_code in (401, 403)


class TestTheShapeTestMatchesTheServersOwnResolver:
    """The shape test must agree with how the ROUTE resolves ``name_or_id``.

    It was a strict dashed-hex regex, which disagreed with ``uuid.UUID`` in both
    directions. Every case below returns the tenant when driven at the real route,
    so flagging them as suspected tenant-NAME oracle probes was a false positive —
    and the follow-up this instrumentation exists to justify (narrowing the auth
    exemption to UUID selectors) would have 401'd them, which ``worker_management``
    converts into a silent ``byo_required`` bypass because it fails open.
    """

    @pytest.mark.parametrize(
        "selector",
        [
            "0195f48bd000700080000000000001".ljust(32, "0")[:32],  # no dashes
            "{0195f48b-d000-7000-8000-000000000001}",  # braced
            "urn:uuid:0195f48b-d000-7000-8000-000000000001",  # urn form
        ],
    )
    def test_an_alternate_uuid_spelling_is_not_an_oracle_probe(
        self, client: TestClient, caplog: pytest.LogCaptureFixture, selector: str
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="auth.middleware"):
            response = client.get(f"/tenants/{selector}/byo-mode")
        # Now that the exemption IS narrowed, the shape test decides the auth
        # boundary — so these spellings must still take the tokenless path, not
        # merely stay silent. A stricter dashed-hex shape would 401 them.
        assert response.status_code != 401, (
            f"{selector!r} is a UUID to `uuid.UUID` and to the route, but the "
            "tokenless exemption refused it"
        )
        assert not [r for r in caplog.records if _MARKER in r.getMessage()], (
            f"{selector!r} is a UUID to `uuid.UUID` and to the route, but was logged "
            "as a suspected tenant-NAME lookup"
        )

    def test_a_trailing_newline_is_not_a_uuid(self) -> None:
        """The permissive direction. ``$`` matches before a trailing newline, so the
        old regex called this a UUID while the resolver rejects it."""
        from auth.middleware import _is_tenant_uuid

        assert not _is_tenant_uuid("0195f48b-d000-7000-8000-000000000001\n")

    def test_a_real_name_is_still_flagged(self) -> None:
        """The discriminating half — the rule must still fire on an actual name."""
        from auth.middleware import _is_tenant_uuid

        assert not _is_tenant_uuid("acme-corp")
        assert _is_tenant_uuid("0195f48b-d000-7000-8000-000000000001")
