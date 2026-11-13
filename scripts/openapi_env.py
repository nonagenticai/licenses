"""The single environment definition the OpenAPI export runs under.

``scripts/export-openapi.py`` (which writes ``docs/openapi.json``) and
``tests/unit/test_openapi_drift.py`` (which fails CI when the snapshot is
stale) both build the spec from this one block. If they each carried their own
copy, the gate would be comparing a spec built under *its* env against a
snapshot built under *the exporter's* env, and the diff would be noise.

Two deliberate choices:

1. **The values are FORCED, not ``setdefault``-ed.** ``os.environ.setdefault``
   loses to an already-exported variable, so an exporter using it produces a
   different document depending on the shell it ran in — and a test using it
   passes locally (clean shell) and fails only in CI (the archetype exports
   ``DATABASE_URL``, ``ENVIRONMENT``, ``SERVICE_NAME``, ... into the job).
   Overriding makes the export a pure function of the source tree.

2. **``DEFAULT_TENANT_ID`` is deliberately NOT here.** It used to be pinned,
   because ``main.custom_openapi`` baked it into the published ``X-Tenant-Id``
   parameter as ``schema.default``/``example``. That was the wrong fix: pinning
   made the export deterministic but left a concrete tenant id in the published
   contract, and the drift gate blind to it. The parameter no longer carries a
   ``default`` or ``example`` at all, so no environment value can reach the
   document by that route and there is nothing left to pin. If a future edit
   reintroduces one, ``tests/unit/test_openapi_drift.py`` fails rather than
   quietly publishing it.

No database connection is made during an export — the spec is built from route
signatures alone — so the ``DATABASE_URL`` below is a syntactically valid
placeholder, not a reachable server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

# KEYCLOAK_URL is deliberately absent: the export forces ENVIRONMENT=development,
# where KeycloakSettings tolerates an empty value. Adding a hardcoded service URL
# would violate the deployment config policy.
EXPORT_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://user:pass@localhost:5432/licenses",  # pragma: allowlist secret
    "ENABLE_AUTH": "false",
    "ENABLE_DOCS": "true",
    "SERVICE_NAME": "licensing-service",
    "VERSION": "1.0.0",
    "ENVIRONMENT": "development",
    "POSTGRES_SSLMODE": "disable",
    "KEYCLOAK_REALM": "secure-apps",
    "KEYCLOAK_CLIENT_ID": "licensing-service",
    "KEYCLOAK_CLIENT_SECRET": "x" * 32,  # pragma: allowlist secret
    "SERVICE_CLIENT_ID": "licensing-service",
    "SERVICE_CLIENT_SECRET": "x" * 32,  # pragma: allowlist secret
}


def apply_export_env(environ: MutableMapping[str, str] | None = None) -> None:
    """Force the export environment onto ``environ`` (default ``os.environ``).

    Must be called BEFORE ``main`` is imported: ``main`` reads ``SERVICE_NAME``
    and ``VERSION`` at module scope.
    """
    target = os.environ if environ is None else environ
    target.update(EXPORT_ENV)


def export_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``base`` (default ``os.environ``) with the export env forced on top.

    For spawning the exporter as a subprocess without mutating this process.
    """
    source = os.environ if base is None else base
    return {**source, **EXPORT_ENV}
