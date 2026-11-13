"""The k8s probe contract: the paths the Deployment probes must exist and be public.

gitops #2553 repointed the licences Deployment's probes. Read from
``clusters/strato/services/licenses/deployment.yaml`` on 2026-08-11:

    startupProbe   -> /health        (deliberately stays: lifespan already hard-
                                      requires the DB, so gating startup on it
                                      too only adds a CrashLoop path)
    readinessProbe -> /health/ready  (real SELECT 1, 503 on failure)
    livenessProbe  -> /health/live   (deliberately DB-free: a DB outage is not a
                                      wedged process, and restarting on it is an
                                      outage amplifier)

Before that, all three pointed at ``/health``, which returns a literal dict and
opens no session — so a pod whose database was gone stayed ``Ready`` and kept
serving platform auth for the whole fleet. Note ``/health`` is STILL probed
(startup), which is a second reason it must stay cheap and SQL-free.

That manifest change makes two previously-cosmetic properties load-bearing, and
this module is where they are pinned:

1. the probe paths RESOLVE. A typo or a renamed route now takes both replicas
   out of rotation on the next roll instead of being a 404 nobody looked at.
2. the probe paths are declared PUBLIC in ``auth.middleware.PUBLIC_ENDPOINTS``.
   Probes carry no bearer token; anything the middleware authenticates is a
   CrashLoop for a probe target.

Honest scope on (2), because the obvious overstatement is wrong: today removing
``/health/ready`` from ``PUBLIC_ENDPOINTS`` would NOT by itself produce a 401,
because ``AuthMiddleware.dispatch`` is default-open and ``/health`` is not under
any entry in ``PROTECTED_PREFIXES``. The membership is still the contract — it is
the declaration a reader and the middleware both consult, and it is what stops
``/health`` from silently becoming protected if a ``/health`` prefix is ever
added. ``/licenses/health`` is the case where membership is directly load-bearing
RIGHT NOW: it sits under the ``/licenses`` protected prefix, so its exemption is
the only reason the public tunnel alias is reachable. Both are asserted, and
``test_licenses_health_alias_401s_without_its_exemption`` proves the second claim
rather than asserting it.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import main
from auth.middleware import PROTECTED_PREFIXES, PUBLIC_ENDPOINTS

# Exactly what the Deployment probes (gitops #2553). Keep in step with
# clusters/strato/services/licenses/deployment.yaml.
PROBE_PATHS = frozenset({"/health", "/health/ready", "/health/live"})

# The public tunnel alias. Not a k8s probe, but the same contract and the one
# whose PUBLIC_ENDPOINTS membership actually gates auth today.
PUBLIC_ALIAS = "/licenses/health"


def _route_paths() -> set[str]:
    return {r.path for r in main.app.routes if isinstance(r, APIRoute)}


class TestProbePathsExist:
    def test_every_probe_path_is_a_real_route(self):
        missing = PROBE_PATHS - _route_paths()
        assert not missing, (
            f"the Deployment probes {sorted(missing)} but no route serves them; "
            "a probe against a 404 takes every replica out of rotation"
        )

    def test_the_public_alias_is_a_real_route(self):
        assert PUBLIC_ALIAS in _route_paths()

    def test_the_route_walk_is_not_vacuous(self):
        """A path-set built from an empty walk would make the above pass blind."""
        assert len(_route_paths()) >= 30


class TestProbePathsArePublic:
    def test_probe_paths_are_declared_public(self):
        missing = PROBE_PATHS - PUBLIC_ENDPOINTS
        assert not missing, (
            f"{sorted(missing)} is probed by kubelet, which sends no bearer token, "
            "but is not in auth.middleware.PUBLIC_ENDPOINTS"
        )

    def test_the_public_alias_is_declared_public(self):
        assert PUBLIC_ALIAS in PUBLIC_ENDPOINTS

    @pytest.mark.parametrize("path", sorted(PROBE_PATHS | {PUBLIC_ALIAS}))
    def test_an_unauthenticated_probe_is_not_rejected(self, client: TestClient, path: str):
        """Behavioural half: no credentials, no 401/403, on every probed path."""
        response = client.get(path)
        assert response.status_code not in (401, 403), (
            f"{path} rejected an unauthenticated request ({response.status_code}); "
            "kubelet and the Cloudflare tunnel both call it without a token"
        )

    def test_the_public_alias_would_be_protected_without_its_exemption(self):
        """Proves the previous test can actually fail — i.e. it is not vacuous.

        ``/licenses/health`` is under the ``/licenses`` protected prefix, so its
        reachability depends entirely on the PUBLIC_ENDPOINTS exemption. Asserting
        that relationship here means the exemption is documented as load-bearing
        rather than looking like decoration.
        """
        assert any(PUBLIC_ALIAS.startswith(p) for p in PROTECTED_PREFIXES)


class TestHealthAndReadyAreDifferentEndpoints:
    def test_health_does_not_report_database_truth_and_ready_does(self, client: TestClient):
        """The whole point of the split, asserted side by side.

        If these two ever report the same thing again, one of them is lying —
        that is exactly the state that let a database-less pod stay Ready.
        """
        health = client.get("/health").json()
        ready = client.get("/health/ready").json()
        assert health["database"] == "not_checked"
        assert ready["database"] == "connected"
        assert health["database"] != ready["database"]
