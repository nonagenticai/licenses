"""`POST /license/introspect` takes no auth, and the flag that would change that.

⛔ THE FINDING. The route requires no credential, has no rate limit, and returns
``tenant_id``, ``license_id``, ``subject_id``, plan and ``byo_mode``. Anyone who can
reach the pod CIDR can turn a captured token into a tenant identity plus an
entitlement profile, and can probe tokens at line rate. ``/licenses/active`` — the
SAME response model — was closed to ``require_internal_or_reader`` in 2026-08 for
exactly this reason (cross-tenant IDOR); this route never was.

⛔ AND WHY THE FLAG SHIPS OFF. MEASURED across the Movies tree: **38 call sites, 36 of
them unauthenticated.** The unauthenticated set includes
``license-authz/src/main.py:718`` — the Istio **ext_authz** enforcer, one POST per mesh
request carrying a token — and **this service's own middleware**, which POSTs to
``127.0.0.1:$PORT/license/introspect`` with no headers to validate an opaque token. It
also includes 26 sibling middlewares, two of them shared libraries (``kag_common``,
``w2v_common``) through which eight further services inherit the call, and five
TS/gateway clients. Two gitops objects assert the endpoint is public and would have to
change with it: ``policies/envoy-filters/ext-authz-enforce.yaml:439`` (path carve-out)
and ``services/oauth2-proxy/deployment.yaml:110`` (``--skip-auth-route``).

So this file's job is mostly the OFF direction: to prove the finding was recorded
without breaking the fleet's authentication primitive. The ON direction is pinned too,
because a flag nobody has ever exercised is not a flag — and in particular the LOOPBACK
EXEMPTION is pinned, since without it the flag cannot be armed in any deployment at all
and arming it would make this service refuse its own auth path.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import main


class TestTheDefaultIsUnchanged:
    def test_the_flag_defaults_off(self) -> None:
        """⛔ Flipping this default is a fleet-wide outage, not a hardening."""
        assert main.INTROSPECT_REQUIRE_AUTH is False

    def test_an_unauthenticated_introspect_still_works(
        self, client: TestClient, full_seed_data
    ) -> None:
        """TODAY'S BEHAVIOUR, pinned. No Authorization header at all."""
        response = client.post("/license/introspect", json={"token": full_seed_data["raw_token"]})
        assert response.status_code == 200, response.text
        assert response.json()["active"] is True

    def test_an_unknown_token_is_still_a_200_inactive(self, client: TestClient) -> None:
        """The route's contract for a bad token is 200 + ``active: false``, not 401.

        Worth pinning beside the flag: if arming it ever turned an INVALID token into a
        401, every caller that branches on ``active`` would start seeing a transport
        error instead of a verdict.
        """
        response = client.post("/license/introspect", json={"token": "no-such-token"})
        assert response.status_code == 200
        assert response.json()["active"] is False

    def test_the_route_is_still_absent_from_the_protected_prefixes(self) -> None:
        """The middleware must not have been made to enforce it as a side effect.

        ``/license/revoke`` is in ``PROTECTED_PREFIXES`` and ``/license`` deliberately
        is NOT — adding the shorter prefix would protect introspect by accident, which
        is the same outage as flipping the flag but with nothing named in the diff.
        """
        from auth.middleware import PROTECTED_PREFIXES

        assert "/license" not in PROTECTED_PREFIXES
        assert "/license/introspect" not in PROTECTED_PREFIXES
        assert "/license/revoke" in PROTECTED_PREFIXES


class TestTheArmedFlag:
    """The ON direction. A flag that has never been exercised is not a flag."""

    def test_an_unauthenticated_non_loopback_caller_is_refused(
        self, client: TestClient, full_seed_data, monkeypatch
    ) -> None:
        monkeypatch.setattr(main, "INTROSPECT_REQUIRE_AUTH", True, raising=True)
        response = client.post("/license/introspect", json={"token": full_seed_data["raw_token"]})
        assert response.status_code == 401, response.text
        assert response.json()["detail"]["error"] == "introspect_requires_auth"

    def test_an_authenticated_caller_is_admitted(
        self, auth_client_admin: TestClient, full_seed_data, monkeypatch
    ) -> None:
        """Arming it must not refuse EVERYONE — that is a different defect.

        Without this, an implementation that 401'd unconditionally would pass the test
        above and look like a working gate.
        """
        monkeypatch.setattr(main, "INTROSPECT_REQUIRE_AUTH", True, raising=True)
        response = auth_client_admin.post(
            "/license/introspect", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is True

    def test_a_tampered_bearer_is_refused(
        self, auth_client_admin: TestClient, full_seed_data, monkeypatch
    ) -> None:
        """⛔ THE DISCRIMINATOR FOR THE TEST ABOVE.

        The route is not under ``PROTECTED_PREFIXES``, so nothing upstream has
        authenticated this caller — the route verifies the bearer itself. Without
        this, an implementation that admitted anyone *carrying* an Authorization
        header would pass both tests above and look like a working gate, which is
        the same defect as 401-ing everyone, in the dangerous direction.

        The signature is replaced, not the header dropped: same three-segment JWT
        shape, same issuer and claims, only the crypto is wrong.
        """
        monkeypatch.setattr(main, "INTROSPECT_REQUIRE_AUTH", True, raising=True)
        head, _, signature = auth_client_admin.headers["Authorization"].rpartition(".")
        response = auth_client_admin.post(
            "/license/introspect",
            json={"token": full_seed_data["raw_token"]},
            headers={"Authorization": f"{head}.{'A' * len(signature)}"},
        )
        assert response.status_code == 401, response.text
        assert response.json()["detail"]["error"] == "introspect_requires_auth"

    def test_an_opaque_license_token_is_not_a_caller_credential(
        self, client: TestClient, full_seed_data, monkeypatch
    ) -> None:
        """A VERIFIED JWT is the only credential this gate accepts, on purpose.

        The middleware also authenticates an opaque license token — by POSTing it
        to this very route — so honouring one here would mean introspecting a token
        to decide whether to introspect a token. The estate's two credentialled
        callers carry a JWT and our own middleware arrives over loopback, so the
        weaker branch buys nothing and costs a recursion.
        """
        monkeypatch.setattr(main, "INTROSPECT_REQUIRE_AUTH", True, raising=True)
        raw_token = full_seed_data["raw_token"]
        response = client.post(
            "/license/introspect",
            json={"token": raw_token},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert response.status_code == 401, response.text
        assert response.json()["detail"]["error"] == "introspect_requires_auth"


def _request_from(host: str | None) -> Request:
    """A REAL ``Request`` whose peer address is ``host``.

    Built from an ASGI scope rather than faked with a ``SimpleNamespace``, because
    ``request.client`` is DERIVED from ``scope["client"]`` — a stub would let the
    helper stop reading the peer address entirely and still pass. ``host=None``
    models a transport that reports no peer at all.
    """
    scope: dict = {"type": "http", "method": "POST", "path": "/license/introspect", "headers": []}
    if host is not None:
        scope["client"] = (host, 54321)
    return Request(scope)


class TestTheLoopbackExemption:
    """⛔ Without this the flag is unarmable, because the first caller is US."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1"])
    def test_loopback_addresses_are_recognised(self, host: str) -> None:
        assert main._is_loopback_client(_request_from(host)) is True

    # One pod-CIDR address (the case the docstring argues about) and two documentation
    # ranges (RFC 5737). The predicate is `ipaddress.is_loopback`, so what these prove is
    # exactly "a routable peer is not exempt" — naming a real host would add nothing.
    @pytest.mark.parametrize("host", ["10.42.3.17", "203.0.113.10", "198.51.100.7"])
    def test_a_routable_address_is_not(self, host: str) -> None:
        """Including an address in the pod CIDR.

        Istio ambient puts in-mesh traffic on the HBONE port and the networkpolicy
        admits 10.42.0.0/16, so "an address belonging to this pod" is reachable by
        other workloads. Only the loopback interface is not.
        """
        assert main._is_loopback_client(_request_from(host)) is False

    @pytest.mark.parametrize("host", [None, "", "not-an-ip", "localhost"])
    def test_an_unparseable_peer_is_not_loopback(self, host: str | None) -> None:
        """⛔ FAIL CLOSED. This function's answer GRANTS an exemption.

        ``localhost`` is in the list deliberately: it is a hostname, not an address,
        and ``request.client.host`` carries an address. Accepting the string would let
        any transport that reports a name bypass the gate.
        """
        assert main._is_loopback_client(_request_from(host)) is False
