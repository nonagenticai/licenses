"""
Licensing Service API

FastAPI backend for managing licenses, tokens, apps, and plans
Integrates with Keycloak for authentication and authorization
No PII stored - only Keycloak subject references. test build
"""

import asyncio
import ipaddress
import logging
import os
from collections.abc import Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, cast
from uuid import UUID

import anyio.from_thread
import anyio.to_thread
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jwt.exceptions import InvalidTokenError
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from pydantic import AwareDatetime, BaseModel, Field, computed_field, field_validator
from sqlalchemy.exc import IntegrityError

from auth.dependencies import (
    INTERNAL_SERVICE_ROLES,
    TENANT_REGISTRY_READER_ROLE,
    require_any_role,
    require_bundle_ensurer,
    require_bundle_token_minter,
    require_internal_or_reader,
    require_internal_service,
    require_role,
)
from auth.jwt_verify import looks_like_jwt, verify_user_jwt
from auth.middleware import (
    _BYO_MODE_OPEN_ROUTES,
    PROTECTED_PREFIXES,
    AuthMiddleware,
    attach_caller_owner,
    realm_owner_mismatch_detail,
    realm_owner_permits,
    register_ownership_hooks,
    register_realm_owner_hook,
    user_from_verified_jwt,
)
from auth.models import User
from auth.tenant import get_tenant_id, require_tenant_id, validate_tenant_uuid
from models import (
    COST_CENTRES,
    NON_HUMAN_SUBJECT_REALMS,
    RESOLUTION_FAULT,
    RESOLUTION_ROW,
    STATEABLE_NON_HUMAN_SUBJECT_REALMS,
    TENANT_LOOKUP_MALFORMED,
    AllTokensExpiredError,
    AllTokensRevokedError,
    App,
    BudgetCurrencyImmutableError,
    BudgetCurrencyTenantArchivedError,
    ClientSafeError,
    DatabaseManager,
    License,
    LongLivedRegistration,
    MalformedSubjectIdentifierError,
    Plan,
    Tenant,
    TenantExternalRefTakenError,
    TenantNameAmbiguousError,
    TenantNameTakenError,
    Token,
    effective_metering_values,
    implicit_long_lived_registration,
    is_db_unreachable,
    redact_dsn,
    service_subject_app_key_problem,
    stated_non_human_subject_problem,
    token_expiry_refusal,
)
from services import bundle_ensure
from services.access_log import AccessLogMiddleware
from services.app_requires import DependencyCycleError, Edge
from services.app_requires import closure as app_requires_closure
from services.expiry_metrics import LicenseExpiryCollector

# ⛔ The billing-mirror teardowns and the small helpers they share live in
# ``services.metering_teardown``, NOT here, and must not be copied back. The
# nightly reaper CronJob (``scripts/reap_expired_licences.py``) cannot import
# this module — ``_validate_production_config()`` runs at module scope and would
# make METERING_SERVICE_URL + CORS_ALLOWED_ORIGINS required env for the job — so
# a definition that lives in ``main`` is a definition the reaper cannot call.
# That is exactly how the reaper shipped with no teardown at all (licences#195).
# They are imported here so every other reference in this file is unchanged.
from services.metering_teardown import (
    METERING_SERVICE_URL,
    _is_retryable_status,
    _is_retryable_transport_error,
    _metering_auth_headers,
    _teardown_quota_config,
    _teardown_token_budget,
)
from services.tenant_subject_invariant import TenantSubjectCollector
from services.tenant_subject_invariant import report_at_boot as report_tenant_subject_violations

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _internal_error(status_code: int, message: str, exc: BaseException, ctx: str) -> HTTPException:
    """Build an HTTPException whose ``detail`` cannot leak the caught exception.

    Interpolating a caught exception into ``detail=`` ships it to the caller: a
    SQLAlchemy/psycopg error stringifies to the failing statement PLUS its bound
    parameters, and a connection error to the DSN (host, port, user, database).
    Nothing downstream sanitises a route-raised ``HTTPException`` below 500 —
    ``http_exception_handler`` scrubs only 5xx — and ``/health/ready`` is in
    ``PUBLIC_ENDPOINTS``, so on that endpoint the leak is unauthenticated.

    ⚠️ This used to say "``global_exception_handler`` re-raises ``HTTPException``
    unchanged". That was FALSE, and the conclusion it supported was right for the
    wrong reason. A route-raised ``HTTPException`` never reaches
    ``global_exception_handler`` at all — starlette routes it to
    ``ExceptionMiddleware``, which is INSIDE the user middleware stack, while the
    ``Exception`` handler is ``ServerErrorMiddleware``'s, outside it. See
    ``global_exception_handler``.

    The real cause goes to the log (with a traceback), the caller gets ``message``
    — a static string chosen by the developer, never derived from ``exc``.

    For a CLIENT fault (4xx) use ``_client_error`` instead — see its docstring.

    ``tests/unit/test_no_exception_leak.py`` enforces the source-level invariant
    this helper exists to make easy to satisfy.
    """
    logger.error("%s: %s", ctx, exc, exc_info=True)
    return HTTPException(status_code=status_code, detail=message)


def _client_error(status_code: int, message: str, exc: BaseException, ctx: str) -> HTTPException:
    """Same sanitisation as ``_internal_error``, at the severity a CLIENT fault deserves.

    A malformed UUID in a request path is not a service error. Logging it at ERROR with
    a full traceback — which is what routing 4xx through ``_internal_error`` did — hands
    any caller a way to drive this service's ERROR rate and traceback volume just by
    sending garbage, and the fleet runs log-level-based health checks on this chain.
    ``origin/main`` logged nothing at all at these sites, so ERROR was a regression in
    both directions: too loud for the operator, caller-triggerable for everyone else.

    WARNING, no traceback: the cause is the request, and the message plus the exception
    text in the log is the whole diagnosis. The caller-visible ``detail`` is still a
    static string, so this is not a weakening of the leak fix — the same AST rule
    covers ``_client_error``'s ``message`` argument.
    """
    logger.warning("%s: %s", ctx, exc)
    return HTTPException(status_code=status_code, detail=message)


# Configuration from environment
SERVICE_NAME = os.getenv("SERVICE_NAME", "licensing-service")
VERSION = os.getenv("VERSION", "1.0.0")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
# No default, deliberately: a localhost fallback reaches nothing inside a pod and
# silently targets whatever Postgres the developer's host happens to run. Unset ⇒
# the service refuses to start (see the DatabaseManager construction below). In the
# cluster every consumer gets it from the ``licenses-env`` Secret via envFrom.
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
REALM_NAME = os.getenv("REALM_NAME", "secure-apps")


def _is_loopback_client(request: Request) -> bool:
    """Is this request from THIS pod, over the loopback interface?

    The one exemption `INTROSPECT_REQUIRE_AUTH` cannot be armed without: this
    service's own `auth/middleware.py::_introspect_license_token` POSTs to
    `http://127.0.0.1:$PORT/license/introspect` to validate an opaque token, and it
    has no credential to present — it is authenticating a caller who is not yet
    authenticated. Requiring auth there would make the service refuse its own
    authentication path.

    ⚠️ `request.client.host` IS SAFE TO TRUST **ONLY** FOR THIS TEST, and only
    because of what it is NOT: it is the peer address of the TCP connection as
    uvicorn sees it, not a header, so it cannot be set by a remote caller. A
    loopback peer means the connection originated inside this pod's network
    namespace. Do NOT extend this to any other decision, and never reach for
    `X-Forwarded-For` here — that IS caller-controlled.

    ⛔ It deliberately does not accept the pod's own routable IP. Istio ambient
    puts in-mesh traffic on the HBONE port and the networkpolicy admits the whole
    pod CIDR (10.42.0.0/16), so "an address belonging to this pod" is reachable by
    other workloads; only the loopback interface is not.

    Uses `ipaddress` rather than a string compare so `::1`, `127.0.0.1` and the
    rest of `127.0.0.0/8` are all covered by one rule. A missing/unparseable peer
    (e.g. an ASGI transport that reports none, as TestClient may) is NOT loopback:
    fail closed, because this function's answer grants an exemption.
    """
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _introspect_caller(http_request: Request) -> User | None:
    """The VERIFIED identity of an introspect caller, or ``None``.

    ⛔ WHY THE ROUTE HAS TO AUTHENTICATE, RATHER THAN READ ``request.state.user``.
    ``AuthMiddleware.dispatch`` populates ``state.user`` ONLY on a path under
    ``PROTECTED_PREFIXES``; for every other path it returns ``await
    call_next(request)`` before it has looked at ``Authorization`` at all. This
    route is deliberately NOT protected — ``/license/revoke`` is the listed
    prefix, not ``/license``, and a test pins that ``/license`` must never be
    added, because the shorter prefix would close introspect fleet-wide with
    nothing named in the diff. So ``state.user`` is ``None`` here for EVERY
    caller, credentialled or not, and a gate reading it alone refused everyone
    the loopback exemption did not cover — including a perfectly good
    ``license-admin``. A gate that 401s every caller is not a gate; it is an
    outage with a feature flag in front of it, and it passes the
    unauthenticated-is-refused test while doing it.

    ``state.user`` is still consulted FIRST, so if this route ever does become
    protected the middleware's answer wins and nothing is verified twice.

    ⚠️ SAME PRIMITIVE AS THE MIDDLEWARE, deliberately: ``user_from_verified_jwt``
    is the function ``dispatch`` authenticates with (signature, expiry and issuer
    checked by ``verify_jwt``). "Authenticated" must not come to mean two
    different things on two paths of one service.

    ⚠️ OFFLOADED, for the reason ``dispatch`` documents at its own call site:
    resolving an unknown ``kid`` goes through PyJWKClient's blocking
    ``urllib`` fetch, and this pod is one process with one event loop. Reachable
    only when ``INTROSPECT_REQUIRE_AUTH`` is armed, which is not the default.

    ⚠️ A VERIFIED JWT IS THE ONLY CREDENTIAL ACCEPTED HERE. The middleware also
    admits an opaque license token — by introspecting it — but taking that branch
    here would mean introspecting a token in order to decide whether to introspect
    a token, recursing through the very route being gated. It is not needed: the
    only callers in the estate that present a credential today (``admin_app``'s
    service-account bearer, the ``n8n-nodes-licenses`` node) carry a JWT, and this
    service's own middleware reaches the route over loopback, which is exempt.
    """
    user = getattr(http_request.state, "user", None)
    if user is not None:
        return cast(User, user)

    scheme, _, token = (http_request.headers.get("Authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return await asyncio.to_thread(user_from_verified_jwt, token.strip())


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse boolean-like environment variables."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Parse an integer environment variable, falling back on anything unusable.

    A retune knob that crashes the pod on a typo is a worse knob than one that
    keeps the shipped default and says so — these two govern how many database
    connections the pod may hold, and the shipped values are already the correct
    ones for the current pgbouncer budget.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError, AttributeError, TypeError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%d is below the minimum %d; using %d", name, value, minimum, default)
        return default
    return value


# ---- Database threadpool governor -----------------------------------------
# TWO separate thread pools in this process reach postgres, and only one of them
# is bounded by default:
#
#   * a sync ``def`` route handler -> ``starlette.run_in_threadpool`` ->
#     ``anyio.to_thread.run_sync`` -> anyio's DEFAULT CapacityLimiter, 40 tokens.
#   * ``asyncio.to_thread``        -> ``loop.run_in_executor(None, …)`` -> the
#     event loop's DEFAULT ThreadPoolExecutor, ``min(32, cpu_count + 4)`` threads.
#     The anyio limiter does NOT bound this one.
#
# The engine uses ``NullPool`` (models.py), so every call in either pool opens a
# NEW physical connection. The two pools added together are therefore this pod's
# client-connection ceiling against pgbouncer.
#
# BUDGET ARITHMETIC.
#   gitops-production/clusters/strato/services/pgbouncer/pgbouncer.ini pins
#   ``licenses … pool_size=20`` — that is PER PGBOUNCER POD, and pgbouncer runs
#   ``replicas: 2``, so there are 40 server connections to db ``licenses`` in
#   total. They are shared by BOTH licences pods (``replicas: 2``), the two
#   CronJobs (expire-stale-licences, metering-reconcile) and any ad-hoc script.
#   Reserve ~8 for the jobs/scripts -> 32 usable -> 16 per licences pod ->
#   8 anyio + 8 loop-executor.
#
# WHY BOUND IT AT ALL. Unbounded the two pools are 40 + 32 = 72 threads per pod,
# i.e. ~144 clients contending for 40 server connections. pgbouncer sets no
# ``query_wait_timeout`` (default 120s), so the overflow does not fail fast — it
# becomes a 120-second hang with no client-side deadline. That is strictly worse
# than the head-of-line blocking this change exists to remove.
#
# ⚠️ ``connect_timeout`` DOES NOT CLOSE THIS. PR #116 added
# ``connect_args={"connect_timeout": 2}`` (models.py), which bounds the TCP
# connect — but pgbouncer ACCEPTS the connection immediately and only then parks
# the query in its wait queue, so the 120s exposure is downstream of the timeout
# and the bound above is still the only thing containing it. Verified live
# 2026-08-12: pgbouncer's ``query_wait_timeout`` is not set (it appears in that
# config only inside a comment) and postgres on the primary reports
# ``statement_timeout = 0`` with ``licenses_user`` carrying no role-level
# override.
#
# Env-overridable so the numbers can be retuned from the Deployment (or when
# pgbouncer's pool_size / replica count changes) without a rebuild. They are
# applied in ``lifespan``, not here: anyio's limiter is a RunVar scoped to the
# RUNNING event loop, so it can only be set from inside the loop.
HANDLER_THREAD_LIMIT = _env_int("HANDLER_THREAD_LIMIT", 8)
TO_THREAD_EXECUTOR_LIMIT = _env_int("TO_THREAD_EXECUTOR_LIMIT", 8)

# Seconds between database-bootstrap retries when the database was UNREACHABLE at
# boot (see ``lifespan``). Deliberately short: the pod is serving 503 on
# ``/health/ready`` and is out of the Service EndpointSlice for the whole window,
# so every second here is a second of reduced auth capacity. Bounded below by 1s
# so a mis-set env var cannot turn this into a busy loop against pgbouncer.
DB_BOOTSTRAP_RETRY_SECONDS = max(1, _env_int("DB_BOOTSTRAP_RETRY_SECONDS", 5))


# Feature flags / docs
ENABLE_DOCS = _env_bool("ENABLE_DOCS", False)
# NB: there is deliberately no DEFAULT_TENANT_ID constant. It only ever decorated
# the published X-Tenant-Id parameter (never request handling), and publishing a
# concrete tenant id is a cross-tenant hazard — see custom_openapi below. The
# DEFAULT_TENANT_ID env var is therefore a no-op and can be dropped from the
# Deployment.
# Enable docs when explicitly toggled, in dev, or under pytest (tests expect docs)
DOCS_ENABLED = (
    (ENVIRONMENT.lower() == "development") or ENABLE_DOCS or ("PYTEST_CURRENT_TEST" in os.environ)
)
ENABLE_AUTH = _env_bool("ENABLE_AUTH", True)

# ⛔ REQUIRE A CREDENTIAL ON `POST /license/introspect`. DEFAULT FALSE, AND THE
# MEASUREMENT SAYS IT MUST STAY FALSE UNTIL A LONG LIST OF REPOS CHANGES.
#
# The route takes no auth at all today and returns `tenant_id`, plan and
# `byo_mode` for any token presented — the token in the body is the only
# credential. That is a real finding. It is ALSO deliberate load-bearing
# infrastructure, and arming this without the work below is a fleet-wide outage of
# the auth path, not a hardening:
#
#   * `license-authz/src/main.py:718` is the Istio **ext_authz** enforcer. It POSTs
#     here unauthenticated for EVERY mesh request carrying a token. It is the
#     hottest caller in the estate and it is the thing that would fail.
#   * **THIS SERVICE'S OWN MIDDLEWARE** does it too:
#     `auth/middleware.py::_introspect_license_token` POSTs to
#     `http://127.0.0.1:$PORT/license/introspect` with no headers. Arming this flag
#     without exempting loopback makes every opaque-license-token caller 401 —
#     the service would refuse its own authentication path.
#   * 26 sibling services carry a copy of the same unauthenticated per-request
#     middleware call (`artifact_service`, `contract_scanner_service`,
#     `discovery-engine`, `media_discovery`, `procedure_management`, `secrets`,
#     `skill_builder`, `storage_service`, `tool_registry`, `vlm_backend`,
#     `worker_management`, `mlx_backend`, `mlx_ingestion_backend`,
#     `scientific_processor`, `webscraper_service`, `mlx_inference`,
#     `nonagenticai_mcp_server`, `coding-ai`, `coding-client`, and the two SHARED
#     libraries `kag_common` and `w2v_common`, through which kag,
#     evidence_review_service, graph_adapter_service, hirag_service,
#     ingestion_service, karma_service, kag_graph_service and nlp_service all
#     inherit it).
#   * TS/gateway callers: `api-gateway/main.py:306`, `commonpractice`
#     (`backend/src/services/license-client.ts:137`, whose own docstring calls the
#     endpoint "public, no auth"), `kanban`, `kanban-mcp`, `license-validator`.
#   * Only TWO callers present a credential: `admin_app`
#     (`services/license_client.py:230`, an SA bearer — and it DEGRADES to
#     unauthenticated when `mint_caller_token()` returns falsy, logging and
#     continuing) and the n8n `n8n-nodes-licenses` node (the licence token itself).
#   * The estate is CONFIGURED for it to be public in two places that must change
#     with it: `gitops-production/clusters/strato/policies/envoy-filters/
#     ext-authz-enforce.yaml:439` carves the path out of ext_authz (not
#     method-restricted), and `services/oauth2-proxy/deployment.yaml:110` passes
#     `--skip-auth-route=POST=^/license/introspect$`.
#
# So this flag is a SWITCH THAT IS NOT READY TO BE THROWN, shipped armed-off on
# purpose: it makes the requirement reviewable and one line away, instead of a
# finding in a document. Before it can go True: exempt loopback (or give the
# middleware a credential), then convert `license-authz`, then the two shared
# libraries — which covers eight services at once — then the remaining middlewares,
# then the TS clients, then the two gitops carve-outs. Nothing else in this repo
# can make that safe.
INTROSPECT_REQUIRE_AUTH = _env_bool("INTROSPECT_REQUIRE_AUTH", False)

# ⛔ RETAIN A DELETED LICENCE INSTEAD OF DESTROYING IT. DEFAULT **TRUE**.
#
# Armed (the default), `DELETE /licenses/user/{subject_id}` stamps the rows
# (migration 036) rather than removing them, revokes their tokens explicitly, and
# requires a blast-radius confirmation. Set `LICENSE_SOFT_DELETE=false` and it
# hard-deletes as it historically did.
#
# ⭐ WHY THE DEFAULT MOVED. It used to default FALSE, and the live deployment does
# not set the variable — so production was running the DESTRUCTIVE path, which is
# the opposite of what a safety flag defaulting off normally buys you. The loss it
# causes is not recoverable by any means this estate has:
# `GET /internal/tenants/{id}/seat-count?as_of=` is the DENOMINATOR of billing's
# per-seat access charge and can only filter rows that STILL EXIST, so one hard
# delete makes every CLOSED period's count silently re-derive SMALLER than the
# invoice was written from, with nothing left to explain the gap. A default whose
# failure mode is "the invoice you already sent can no longer be reproduced" is the
# wrong default regardless of how careful its callers are.
#
# ⛔ THE OLD COMMENT'S OBJECTION WAS REAL AND HAD TO BE CLOSED FIRST — it is, and
# this flip is not valid without it. It said: "a retained row is still returned by
# `GET /internal/licenses` and `GET /licenses`, because nothing filters `deleted_at`
# on the list surfaces yet. Arming this would make deleted licences REAPPEAR to
# their consumers — including billing." That was correct. `list_licenses` now takes
# `include_deleted` (default False) and both list surfaces expose it, so an armed
# soft delete shows its consumers exactly what a hard delete showed them: nothing.
# ⚠️ Do not re-flip this to False as a "safe rollback" without checking that the
# read half is still in place — off, the read filter is inert, but on, removing the
# read filter is what resurrects deleted rows.
#
# ⚠️ THE HARD PATH IS STILL REACHABLE AND IS NO LONGER SILENT. Setting the flag
# false restores the historical unconfirmed destroy, because a caller who has
# deliberately opted back in must not be broken — but it now emits the
# `licence_delete_confirmed … mode=hard` ERROR line with actor, counts and reason
# first. The row cannot be the audit record when the row is what is being
# destroyed, so the log line is; that is the same mechanism `delete_app` has used
# for the same reason, and this service has no audit table to write to instead.
LICENSE_SOFT_DELETE = _env_bool("LICENSE_SOFT_DELETE", True)

# CORS configuration: dev-friendly default origins (localhost only).
# Production deployments MUST set CORS_ALLOWED_ORIGINS explicitly — the dev
# defaults are NEVER used in production (see _validate_production_config).
# Deployment and developer-machine hostnames are environment-specific and must
# come from the environment; there are deliberately no hardcoded non-local
# fallbacks. A developer serving a UI from another host on the LAN sets
# CORS_ALLOWED_ORIGINS to that origin.
DEFAULT_CORS_ORIGINS_DEV: list[str] = [
    # Local development
    "http://localhost:3000",
    "https://localhost:3000",
    "http://localhost:8008",
    "https://localhost:8008",
    "http://localhost:8687",
    "https://localhost:8687",
]


def _resolve_cors_origins() -> list[str]:
    """Resolve allowed CORS origins from CORS_ALLOWED_ORIGINS env var.

    When the env var is set (non-empty), parse as a comma-separated list,
    stripping whitespace and dropping empty entries. When unset or empty
    after parsing in non-production environments, fall back to
    DEFAULT_CORS_ORIGINS_DEV. In production, an empty/missing env var is
    caught by _validate_production_config() at startup.
    """
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    parsed = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if parsed:
        return parsed
    if ENVIRONMENT.lower() == "production":
        # Returned but production validator will fail-fast before serving.
        return []
    return DEFAULT_CORS_ORIGINS_DEV


CORS_ALLOWED_ORIGINS = _resolve_cors_origins()


# Service URLs / origins that MUST be set from gitops env in production.
# Empty values trigger a fail-loud RuntimeError at startup so misconfigured
# pods never silently serve with wrong/missing upstream addresses.
_REQUIRED_ENV_IN_PRODUCTION: tuple[tuple[str, str], ...] = (
    ("METERING_SERVICE_URL", METERING_SERVICE_URL),
    ("CORS_ALLOWED_ORIGINS", os.getenv("CORS_ALLOWED_ORIGINS", "")),
    ("DATABASE_URL", os.getenv("DATABASE_URL", "")),
)


def _validate_production_config() -> None:
    """Fail loud when required env vars are missing in production."""
    if ENVIRONMENT.lower() != "production":
        return
    missing = [name for name, value in _REQUIRED_ENV_IN_PRODUCTION if not value]
    if missing:
        raise RuntimeError(
            "licenses-service missing required env vars in production: "
            f"{missing}. Set them in the Deployment's env block, or in your "
            ".env file if you are running it directly."
        )


_validate_production_config()

if not DATABASE_URL:
    raise RuntimeError(
        "licenses-service: DATABASE_URL is not set. There is no default database — "
        "set DATABASE_URL (in the cluster it comes from the licenses-env Secret; for "
        "an import-only step such as the OpenAPI export or an image smoke, pass an "
        "explicit non-connectable URL)."
    )

# Initialize components
db_manager = DatabaseManager(DATABASE_URL)

# Migration 050: the auth middleware resolves each verified caller's OWNER from
# api_client and checks X-Tenant-Id retargets against ownership/grants. The
# lambdas read the module-global ``db_manager`` at CALL time, so a test (or the
# lifespan) that swaps it is honoured.
register_ownership_hooks(
    lambda realm, client_id: db_manager.resolve_api_client(realm, client_id),
    lambda owner_key, tenant_id: db_manager.owner_can_access_tenant(owner_key, tenant_id),
)
# Migration 072 (realm-per-app P0): realm -> owning app, for the cross-realm tenant check.
register_realm_owner_hook(lambda realm: db_manager.resolve_realm_owner(realm))


# ---------------------------------------------------------------------------
# Tenant ownership (migration 050) — the authorization helpers every tenant /
# licence / grant / bundle WRITE goes through. All BLOCKING (DB); async routes
# call them via asyncio.to_thread.
# ---------------------------------------------------------------------------


def _ensure_owner_resolved(user: User) -> None:
    """Resolve the owner for a User that did not come through the JWT middleware
    path (e.g. a test-constructed User). Idempotent and cheap when already set."""
    if user.owner_key is None and not user.cross_owner and user.azp:
        attach_caller_owner(user)


def _authorize_token_revoke(user: User, raw_token: str, tenant_id: str) -> None:
    """Revocation rule for ``POST /license/revoke`` by raw token, decided over
    EVERY licence of ``tenant_id`` carrying the token — exactly the rows
    ``revoke_token_for_tenant`` writes — via ``_authorize_licence_revoke`` (issuer
    per app, or the tenant owner over bundle-minted rows). No row on this tenant
    is left to the revoke itself, which answers "not found".

    It used to look up ONE row by hash (non-unique) and authorize that row's app
    only: when the pick landed on another tenant's twin it returned without any
    check, and when it landed on an app the caller issues the twins on other
    apps were revoked unchecked — both measured as a 200 ``revoked:true`` on a
    non-bundle licence the caller does not issue."""
    _require_known_owner(user)
    twins = db_manager.licences_for_token_hash_in_tenant(
        Token.hash_token(raw_token), str(tenant_id)
    )
    if not twins:
        return
    _authorize_licence_revoke(user, twins, tenant_id, "token_revoke")


def _audit_cross_owner(user: User, action: str, tenant: Tenant | None, detail: str = "") -> None:
    """THE audit record of a break-glass action (this service has no audit table;
    the log line is the record — same mechanism as ``budget_currency_changed``)."""
    logger.warning(
        "cross_owner_action: action=%s tenant=%s tenant_owner=%s caller_owner=%s azp=%s "
        "realm=%s sub=%s%s at=%s",
        action,
        tenant.id if tenant is not None else None,
        tenant.owner_key if tenant is not None else None,
        user.owner_key,
        user.azp,
        user.realm,
        user.sub,
        f" {detail}" if detail else "",
        datetime.now(UTC).isoformat(),
    )


#: Realm roles admitted to the OWNER-SCOPED tenant writes: register/create a tenant,
#: grant/revoke/list grants, and per-APP settings (per-app BYO mode, offering
#: provision/unprovision). Since migration 050 the protection on these routes is
#: owner scoping (api_client -> owner_key, plus tenant_app_grant for per-app
#: settings), not the role: holding ``license-admin`` no longer implies reach
#: across apps. ``license-admin`` alone locked out owners that by design must
#: register their own tenants and only hold ``license-manager`` — merqi
#: (secure-apps/control-service-account: bundle-provisioner + license-manager),
#: w2v (portal/w2v-backend: license-manager) and fr (portal/fr-backend:
#: license-manager) — every call was a 403 "Role 'license-admin' required". The tenant-WIDE
#: writes (rename, archive/unarchive, tenant BYO, cost centre, budget currency) use the
#: same roles but stay OWNER-ONLY via ``_resolve_owned_tenant`` (grantee: 403
#: ``tenant_owner_only``). Switching an existing budget currency still needs the
#: separate ``platform-admin`` capability.
OWNER_WRITE_ROLES = ("license-admin", "license-manager")


def _require_known_owner(user: User) -> str:
    """The caller's owner, or 403 ``unknown_api_client``. Never a default owner."""
    _ensure_owner_resolved(user)
    if user.owner_key is None:
        logger.warning(
            "unknown_api_client: refused write realm=%s azp=%s sub=%s",
            user.realm,
            user.azp,
            user.sub,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "unknown_api_client",
                "message": (
                    "this client is not registered as an owning app (api_client); "
                    "tenant and licence writes are refused"
                ),
            },
        )
    return user.owner_key


def _tenant_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")


def _resolve_tenant_for_caller(user: User, name_or_id: str, action: str = "tenant_read") -> Tenant:
    """Owner-scoped resolution: own tenants by name or id; granted tenants by id
    only; anything else 404 (indistinguishable from absent). A cross_owner caller
    reaches any tenant, audited under ``action``."""
    _ensure_owner_resolved(user)
    try:
        tenant = db_manager.get_tenant_scoped(
            name_or_id, user.owner_key, cross_owner=user.cross_owner
        )
    except TenantNameAmbiguousError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_name_ambiguous",
                "name": name_or_id,
                "message": "this tenant name is used by several owning apps; use the id",
            },
        ) from exc
    if tenant is None:
        raise _tenant_not_found()
    if user.cross_owner and tenant.owner_key != user.owner_key:
        _audit_cross_owner(user, action, tenant)
    return tenant


def _resolve_owned_tenant(user: User, name_or_id: str, action: str) -> Tenant:
    """Owner-only WRITE resolution for TENANT-WIDE settings (rename, archive,
    tenant-level BYO mode, cost centre, budget currency, grants). A grantee gets
    403 ``tenant_owner_only``; another owner's tenant is 404. Per-APP settings
    (per-app BYO, offerings) go through ``_resolve_tenant_for_app_setting``."""
    _require_known_owner(user)
    try:
        tenant = db_manager.get_tenant_scoped(
            name_or_id, user.owner_key, cross_owner=user.cross_owner
        )
    except TenantNameAmbiguousError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_name_ambiguous",
                "name": name_or_id,
                "message": "this tenant name is used by several owning apps; use the id",
            },
        ) from exc
    if tenant is None:
        raise _tenant_not_found()
    if tenant.owner_key != user.owner_key:
        if user.cross_owner:
            _audit_cross_owner(user, action, tenant)
            return tenant
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tenant_owner_only",
                "message": "only the tenant's owning app may do this; a grant does not",
            },
        )
    return tenant


#: Per-app setting actions that only REMOVE state: a revoked bundle licence still
#: admits the tenant owner for these (offboarding cleanup), never for a set/provision.
_APP_SETTING_CLEAR_ACTIONS = frozenset({"tenant_app_byo_mode_clear", "tenant_offering_unprovision"})


def _resolve_tenant_for_app_setting(
    user: User, name_or_id: str, app_key: str, action: str
) -> Tenant:
    """WRITE resolution for a PER-APP tenant setting (per-app BYO mode, offering
    provision / unprovision — all keyed on ``(tenant, app_key)``).

    The tenant's owner may set it for an app it ISSUES, or for an app its own
    bundle brought onto the tenant (a licence there with ``bundle_id`` set): an
    ACTIVE one to set or provision, any status to clear or unprovision (so
    offboarding can still clean up after revoking, but a revoked bundle cannot
    be used to re-provision an offering and its billing markup); otherwise 403
    ``app_not_administrable_by_caller`` (``_authorize_app_admin``). Another owner
    may set it ONLY for an app it ISSUES on a tenant it holds an ACTIVE grant on — the same rule as
    minting that app's licence there (``_authorize_issuance``), so a grantee that
    may mint ``commonpractice`` on ``system`` may also set ``commonpractice``'s BYO
    posture there. 403 ``app_not_issuable_by_caller`` (grantee, not an issuer) /
    ``tenant_not_accessible`` (no grant). Tenant-WIDE settings (rename, archive,
    tenant-level BYO, cost centre, budget currency, grants) stay owner-only
    (``_resolve_owned_tenant``). A name resolves only within the caller's own
    owner; an unknown tenant is 404. cross_owner bypasses, audited.
    """
    owner = _require_known_owner(user)
    try:
        UUID(str(name_or_id))
        is_id = True
    except ValueError, TypeError:
        is_id = False
    if is_id and not user.cross_owner:
        tenant = db_manager.get_tenant(name_or_id)
    else:
        try:
            tenant = db_manager.get_tenant_scoped(name_or_id, owner, cross_owner=user.cross_owner)
        except TenantNameAmbiguousError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "tenant_name_ambiguous",
                    "name": name_or_id,
                    "message": "this tenant name is used by several owning apps; use the id",
                },
            ) from exc
    if tenant is None:
        raise _tenant_not_found()
    app_row = db_manager.get_app_by_key(app_key)
    if tenant.owner_key == owner:
        if app_row is None:
            # Unknown app on the caller's own tenant: no write can happen, so the
            # route keeps answering its own 404 ("Tenant or app not found").
            return tenant
        app_id = cast(UUID, app_row.id)
        if owner in db_manager.app_issuers(app_id):
            return tenant
        if db_manager.tenant_has_bundle_licence(
            cast(UUID, tenant.id), app_id, active_only=action not in _APP_SETTING_CLEAR_ACTIONS
        ):
            # The owner ensured the bundle that brought this app onto its own
            # tenant (``_bundle_owner_may_revoke``'s reasoning): its per-app
            # posture is the owner's to manage — CP on a bundle app it does not
            # issue (e.g. ``workers``).
            return tenant
        # Owning the tenant is not owning the app: issuer or audited cross_owner.
        _authorize_app_admin(user, app_id, action, tenant)
        return tenant
    if user.cross_owner:
        _audit_cross_owner(user, action, tenant, f"app_key={app_key}")
        return tenant
    if app_row is None:
        # Not an issuer of an app that does not exist: refused the same way, so a
        # non-owner cannot enumerate the catalogue through this route.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "app_not_issuable_by_caller",
                "message": f"owner {owner!r} may not issue this app's licences",
            },
        )
    _authorize_issuance(user, app_row.id, tenant.id, action)
    return tenant


def _authorize_bundle_ensurer(user: User, tenant: Tenant, bundle_key: str) -> None:
    """Bundle-ensure rule (migration 052): the caller's owner must be a
    ``bundle_ensurer`` of the bundle. 403 ``bundle_not_ensurable_by_caller``.

    An unknown bundle is left to ``services.bundle_ensure``'s own refusal (bundle
    keys are not secret). cross_owner bypasses, audited.
    """
    owner = _require_known_owner(user)
    bundle = db_manager.get_bundle_by_key(bundle_key)
    if bundle is None:
        return
    if owner in db_manager.bundle_ensurers(cast(UUID, bundle.id)):
        return
    if user.cross_owner:
        _audit_cross_owner(user, f"bundle_ensure_unlisted:{bundle_key}", tenant)
        return
    logger.warning(
        "bundle_not_ensurable_by_caller: owner=%s bundle=%s tenant=%s azp=%s",
        owner,
        bundle_key,
        tenant.id,
        user.azp,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "bundle_not_ensurable_by_caller",
            "tenant_id": str(tenant.id),
            "bundle": bundle_key,
            "message": f"owner {owner!r} may not ensure bundle {bundle_key!r}",
        },
    )


def _authorize_issuance(user: User, app_id: Any, tenant_id: Any, action: str) -> None:
    """Licence issuance / revocation rule (migration 050).

    The caller's owner must ISSUE the app (app_issuer) AND own the tenant or hold
    an active grant on it. 403 ``app_not_issuable_by_caller`` /
    ``tenant_not_accessible``. A tenant that does not exist is left to the
    route's own (pre-050) refusal. cross_owner bypasses, audited.
    """
    owner = _require_known_owner(user)
    issuers = db_manager.app_issuers(app_id)
    tenant = db_manager.get_tenant(str(tenant_id)) if tenant_id is not None else None
    issuable = owner in issuers
    accessible = tenant is None or db_manager.owner_can_access_tenant(owner, str(tenant.id))
    if issuable and accessible:
        return
    if user.cross_owner:
        _audit_cross_owner(user, action, tenant, f"app_id={app_id}")
        return
    if not issuable:
        logger.warning(
            "app_not_issuable_by_caller: action=%s owner=%s app_id=%s tenant=%s azp=%s",
            action,
            owner,
            app_id,
            tenant_id,
            user.azp,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "app_not_issuable_by_caller",
                "message": f"owner {owner!r} may not issue this app's licences",
            },
        )
    logger.warning(
        "tenant_not_accessible: action=%s owner=%s tenant=%s azp=%s",
        action,
        owner,
        tenant_id,
        user.azp,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "tenant_not_accessible",
            "message": "the tenant is neither owned by nor granted to the calling app",
        },
    )


def _authorize_app_admin(
    user: User, app_id: Any, action: str, tenant: Tenant | None = None
) -> None:
    """App-admin rule for the per-app catalogue writes (plans, offerings, app delete).

    The caller's owner must ISSUE the app (``app_issuer``, migration 050) — the
    same ownership that gates minting its licences. ``license-admin`` alone is a
    role, not ownership: without this any license-admin client could create,
    alter or delete plans on another owner's app (and a foreign plan could then
    be picked by that owner's bundle ensure) or delete the app outright.
    403 ``app_not_administrable_by_caller``. cross_owner bypasses, audited. The
    route resolves the app (404) BEFORE calling this.
    """
    owner = _require_known_owner(user)
    if owner in db_manager.app_issuers(app_id):
        return
    if user.cross_owner:
        _audit_cross_owner(user, action, tenant, f"app_id={app_id}")
        return
    logger.warning(
        "app_not_administrable_by_caller: action=%s owner=%s app_id=%s azp=%s",
        action,
        owner,
        app_id,
        user.azp,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "app_not_administrable_by_caller",
            "message": f"owner {owner!r} does not issue this app and may not administer it",
        },
    )


def _bundle_owner_may_revoke(
    user: User, licences: Sequence[License], tenant_id: Any, *, allow_dependency: bool = False
) -> Tenant | None:
    """REVOKE-ONLY widening of ``_authorize_issuance``: the tenant's OWNER may
    revoke licences its bundles minted even when it does not issue their app.

    A bundle is ensured by the tenant's owner (``_authorize_bundle_ensurer``), so
    the licences it minted (``License.bundle_id`` set) are the owner's to tear
    down — the real case is CP offboarding a tenant (archive) and then revoking
    the inert bundle licences of apps it does not issue (e.g. ``workers``).

    Returns the tenant only when EVERY licence is bundle-minted, on ``tenant_id``,
    and the caller's owner owns that tenant (a grant is not enough; archived
    tenants count — that is the use); otherwise None. One non-bundle row refuses
    the whole set, so this never reaches a row the owner could not mint. Never
    used on a mint or token-issue path. It decides only: the ``bundle_owner_revoke``
    audit lines are written by ``_authorize_licence_revoke`` once the WHOLE call
    is authorized.

    ``allow_dependency`` (``POST /license/revoke`` by licence id only): a DEPENDENCY
    licence (``is_dependency``, ``bundle_id`` NULL — shared, so it names no bundle)
    is admitted too. The route then refuses 409 ``dependency_still_required`` when a
    remaining active licence on the tenant still requires it, checked in the
    revoke's own transaction.
    """
    owner = user.owner_key
    if owner is None or not licences or tenant_id is None:
        return None
    tenant = db_manager.get_tenant(str(tenant_id))
    if tenant is None or tenant.owner_key != owner:
        return None
    if any(
        (lic.bundle_id is None and not (allow_dependency and lic.is_dependency))
        or str(lic.tenant_id) != str(tenant.id)
        for lic in licences
    ):
        return None
    return tenant


def _authorize_licence_revoke(
    user: User,
    licences: Sequence[License],
    tenant_id: Any,
    action: str,
    *,
    allow_dependency: bool = False,
) -> None:
    """Revocation rule: ``_authorize_issuance`` per app, OR — for an app the
    caller does not issue — ``_bundle_owner_may_revoke`` over that app's rows.

    The ``bundle_owner_revoke`` audit lines are emitted only after EVERY app has
    passed: logging per app as it was admitted left a "revoke" record behind
    when a later app of the same call was refused 403 and nothing was revoked.

    ``allow_dependency`` admits DEPENDENCY licences through the owner widening; the
    "still required" refusal is NOT decided here but by the route, for every caller."""
    owner = _require_known_owner(user)
    by_app: dict[str, list[License]] = {}
    for lic in licences:
        by_app.setdefault(str(lic.app_id), []).append(lic)
    admitted: list[tuple[Tenant, list[License]]] = []
    for app_id in sorted(by_app):
        if not user.cross_owner and owner not in db_manager.app_issuers(UUID(app_id)):
            tenant = _bundle_owner_may_revoke(
                user, by_app[app_id], tenant_id, allow_dependency=allow_dependency
            )
            if tenant is not None:
                admitted.append((tenant, by_app[app_id]))
                continue
        _authorize_issuance(user, UUID(app_id), tenant_id, action)
    for tenant, rows in admitted:
        for lic in rows:
            logger.info(
                "bundle_owner_revoke: action=%s azp=%s realm=%s owner=%s tenant=%s "
                "licence=%s bundle=%s app_id=%s tenant_archived=%s dependency=%s",
                action,
                user.azp,
                user.realm,
                owner,
                tenant.id,
                lic.id,
                lic.bundle_id,
                lic.app_id,
                tenant.archived_at is not None,
                bool(lic.is_dependency),
            )


# ---------------------------------------------------------------------------
# Fire-and-forget task durability
# ---------------------------------------------------------------------------
# The event loop keeps only a WEAK reference to a running task, so a bare
# ``asyncio.create_task(coro)`` whose return value is discarded can be
# garbage-collected mid-flight — the documented asyncio footgun. Every
# fire-and-forget spawn in this module goes through ``_spawn_background_task``,
# which holds a STRONG reference in this set until the task completes and the
# done-callback discards it (so the set cannot grow without bound).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

# One greppable marker for every way metering provisioning can fail.
#
# This service has NO Prometheus scrape annotation, so a counter here would be
# unobservable in production — a log line is the only channel that reaches an
# operator. It also has to be an ERROR, not a WARNING: licenses runs under
# uvicorn, whose access log carries HTTP statuses only, and this work happens
# AFTER the 201 has already gone back to the caller. Without a loud line the
# failure leaves no trace anywhere.
METERING_FAILURE_MARKER = "METERING_PROVISIONING_FAILED"

# Tenants billing_service will NEVER hold a token budget for. MIRRORS billing
# `src/tenancy.py` SENTINEL_TENANT_IDS exactly ("fabricated tenants: they
# resolve, they are never registered, they never bill"); POST /api/budget 422s
# for them by design. Budget pushes for these are skipped, not failed (#234).
# ⛔ Do NOT widen this to "any 422 unregistered": that would silently hide a
# genuinely mis-tenanted licence, which is exactly what the failure must catch.
BILLING_SENTINEL_TENANT_IDS: frozenset[str] = frozenset({"00000000-0000-0000-0000-000000000001"})

# Repair path quoted in the failure logs so the operator does not have to look
# it up. Kept next to the marker so the two can never drift apart.
METERING_RECONCILE_PATH = "/licenses/admin/metering/reconcile"


def _background_task_done(task: asyncio.Task[Any]) -> None:
    """Drop the strong reference and make any non-clean finish LOUD."""
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        # The realistic cause: the pod was terminated mid-rollout while the
        # provisioning POSTs were in flight. Silently losing this is exactly how
        # a licence ends up created-but-unmetered.
        logger.error(
            "%s background task %s was CANCELLED before it finished — a licence may "
            "be unmetered; repair with POST %s",
            METERING_FAILURE_MARKER,
            task.get_name(),
            METERING_RECONCILE_PATH,
        )
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "%s background task %s raised %r — a licence may be unmetered; repair with POST %s",
            METERING_FAILURE_MARKER,
            task.get_name(),
            exc,
            METERING_RECONCILE_PATH,
            exc_info=exc,
        )


def _spawn_background_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule ``coro`` fire-and-forget while holding a strong reference to it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_background_task_done)
    return task


def _spawn_billing_teardowns_from_thread(license_ids: list[str], tenant_id: str) -> None:
    """Spawn BOTH billing teardowns for each licence, from a SYNC route's thread.

    The sync-route counterpart of the two ``_spawn_background_task`` calls on the
    single-licence revoke path (``revoke_token_or_license``): the quota-config
    mirror AND the ``license_sync`` token budget, as separate tasks so one failing
    does not suppress the other. A sync ``def`` route runs in anyio's worker
    thread, where there is no running loop for ``asyncio.create_task``, so the
    spawn is hopped onto the event loop with ``anyio.from_thread.run_sync``.

    Same contract as that path: spawned, never awaited, never raises into the
    caller — the licence write is already committed and is the security-critical
    half; billing's mirror is derived state with its own reconcile backstop.
    """
    if not license_ids:
        return

    def _spawn() -> None:
        for license_id in license_ids:
            _spawn_background_task(
                _teardown_quota_config(license_id=license_id, tenant_id=tenant_id)
            )
            _spawn_background_task(
                _teardown_token_budget(license_id=license_id, tenant_id=tenant_id)
            )

    try:
        anyio.from_thread.run_sync(_spawn)
    except Exception:
        logger.error(
            "BILLING_TEARDOWN_SPAWN_FAILED: tenant=%s licences=%s — billing's quota "
            "config and token budget for these deleted licences were NOT torn down; "
            "billing's licence-budget reconcile is the backstop",
            tenant_id,
            license_ids,
            exc_info=True,
        )


def _stamp_license_metering(license_id: str, columns: dict[Any, Any], what: str) -> bool:
    """Write metering bookkeeping columns for ``license_id``.

    Returns True when a row was stamped. Never raises: a bookkeeping failure
    must not turn into a lost licence — it just leaves the row NULL, which the
    reconciler will pick up on its next sweep.
    """
    session = db_manager.get_session()
    try:
        updated = (
            session.query(License)
            .filter(License.id == UUID(license_id))
            .update(columns, synchronize_session=False)
        )
        session.commit()
        return bool(updated)
    except Exception:
        session.rollback()
        logger.error(
            "%s could not record %s for license %s — the work SUCCEEDED but is not "
            "recorded, so the reconciler will re-examine it on its next sweep (safe: "
            "the sweep reads billing before it writes anything)",
            METERING_FAILURE_MARKER,
            what,
            license_id,
            exc_info=True,
        )
        return False
    finally:
        session.close()


def _mark_metering_provisioned(license_id: str) -> bool:
    """Record that metering provisioning COMPLETED for ``license_id``.

    ⚠️ This column is NOT a coverage signal — migration 018 baselined it across
    the whole table, so a non-NULL value can equally mean "018 stamped this row
    sight unseen". ``_mark_metering_verified`` is the one that means billing was
    actually asked. See migration 019.
    """
    return _stamp_license_metering(
        license_id,
        {License.metering_provisioned_at: datetime.now(UTC)},
        "metering_provisioned_at",
    )


def _mark_metering_verified(license_id: str) -> bool:
    """Record that billing was ASKED about ``license_id`` and holds what it owes.

    Stamps ``metering_verified_at`` and NOTHING ELSE. Only ever called after
    billing itself answered; never on a dry run.

    ⚠️ It deliberately does NOT touch ``metering_provisioned_at``, and an earlier
    revision of this function did. Two reasons that write was wrong:

    * On the already-covered path the sweep performs no push at all, so stamping
      a "provisioned" time would record provisioning THIS SERVICE NEVER DID —
      the same species of false instrumentation this whole change exists to
      remove.
    * ``metering_provisioned_at`` is the surviving evidence of migration 018's
      blanket baseline. Re-measured in prod 2026-08-10:
      ``count(metering_provisioned_at) = 208`` of 208 rows — i.e. NOT ONE row is
      NULL — and 205 of those 208 share a single transaction timestamp
      (2026-08-09 20:30:20.780955+00), the other three being genuinely observed
      create-path stamps. Both numbers matter: 208/208 non-NULL is why the
      column can never answer "is this licence covered", and 205-on-one-timestamp
      is how anyone can still tell a baselined stamp from an observed one. A
      single sweep over the estate would overwrite that histogram permanently
      and destroy the proof.

    The two columns are allowed to disagree. That disagreement is information.
    """
    return _stamp_license_metering(
        license_id,
        {License.metering_verified_at: datetime.now(UTC)},
        "metering_verified_at",
    )


def _billing_write_succeeded(status_code: int | None) -> bool:
    """Whether a ``_provision_*`` helper's returned status means billing accepted the write.

    Explicit range rather than ``if status_code``: the helpers return an ``int |
    None`` status now, and a stray truthy value (a legacy ``True``, a ``0``)
    must read as FAILURE and be loud, not sail through as success.
    """
    return status_code is not None and 200 <= status_code < 300


def _restore_tenant_app_posture(
    name_or_id: str,
    app_key: str,
    prior: str | None,
    prior_offering_code: str | None = None,
) -> None:
    """Undo one ``set_tenant_app_byo_mode`` write, restoring what was there before.

    ``prior`` is the ``prior_byo_mode`` that write returned: ``None`` means it
    CREATED the row (so the undo is a delete), any string means it OVERWROTE an
    existing override (so the undo is to write that value back).

    ``prior_offering_code`` is restored alongside it. Restoring the posture alone
    would leave the FAILED provision's offering code attributed to the tenant's
    own prior posture — a row that says it came from an offering that never
    successfully applied.

    The rollback used to be an unconditional
    ``db_manager.clear_tenant_app_byo_mode(...)``. Paired with an upsert forward
    write, that is not a rollback — it is data destruction: a tenant with a
    deliberate per-product posture lost it permanently the first time a
    re-provision failed on the billing hop.

    **A rollback that does nothing is logged at ERROR, and a rollback that raises
    is allowed to propagate.** Both are only reachable if the tenant, the app or
    the override row is deleted between the forward write and this call, but the
    caller's very next act is to raise a 502 whose detail tells the operator the
    posture "was rolled back". If that is not true, silence here would make the
    502 itself the misinformation — so this never swallows the failure. A 500 is
    the correct answer to "the rollback failed"; it is strictly worse news than
    the 502 it replaces.
    """
    if prior is None:
        if not db_manager.clear_tenant_app_byo_mode(name_or_id, app_key):
            logger.error(
                "posture rollback CLEARED NOTHING for tenant=%s app=%s — the override row "
                "this request created is already gone; the 502 below claims a rollback that "
                "did not happen",
                name_or_id,
                app_key,
            )
    elif (
        db_manager.set_tenant_app_byo_mode(
            name_or_id, app_key, prior, offering_code=prior_offering_code
        )
        is None
    ):
        logger.error(
            "posture rollback FAILED for tenant=%s app=%s: cannot restore prior posture %r "
            "because the tenant or app no longer resolves; the tenant is left carrying this "
            "request's posture instead of its own",
            name_or_id,
            app_key,
            prior,
        )


async def _fetch_license_sync_budget_ids(
    client: Any,
    *,
    tenant_id: str,
    headers: dict[str, str],
) -> dict[str, int | None] | None:
    """License ids for which billing already holds an ACTIVE license_sync budget.

    Maps each covered licence id to the ``budget_tokens`` of its active MONTHLY
    ``license_sync`` row (the period ``_push_token_budget`` writes), or ``None``
    when the licence is covered but no monthly value could be read. The value is
    what the reconcile sweep compares against the licence's EFFECTIVE budget, so a
    raised/lowered cap that never reached billing is re-pushed (2026-10-15: a KAG
    licence's raised cap kept being refused at the ingress until billing restarted,
    because nothing ever re-pushed an existing licence's budget).

    Returns ``None`` when billing did not answer — "unknown", which is NOT the
    same as "empty" and must never be treated as one: creating a budget on an
    unknown state is how you get a duplicate row.

    Reads ``GET /api/budget/list/{tenant_id}``, which billing gates with
    ``require_tenant`` + ``enforce_tenant_isolation``. ``require_tenant`` returns
    the ``X-Tenant-Id`` header verbatim, so the isolation check passes exactly
    when the header matches the path — which it does here, because
    ``_metering_auth_headers`` sets the header to this licence's tenant. Verified
    live against the deployed billing pod on 2026-08-10 (200, envelope
    ``{"data": [...], "meta": {...}}``).

    Only ``active`` rows with ``source='license_sync'`` count as coverage:

    * ``active=False`` is billing's soft-delete (see
      ``deactivate_orphan_license_sync_budgets``) and enforces nothing.
    * ``source='manual'`` / ``'topup'`` rows STACK by design and are frequently
      tenant-wide with ``license_id: null`` — prod tenant
      019de842-c92a-7e37-8fdd-eb0d8c5396f9 holds exactly one such row (40M
      tokens) while 21 of its licences owe a licence-keyed budget. Counting
      those as coverage would call all 21 covered.
    """
    import httpx

    url = f"{METERING_SERVICE_URL}/api/budget/list/{tenant_id}"
    # One bounded retry, matching _push_token_budget / _push_quota_config. This
    # read is per-TENANT, so a single blip does not cost one licence — it sends
    # every licence under that tenant to `unknown` and repairs none of them (21
    # licences for the largest prod tenant). Fail-closed is right; failing closed
    # on the first transient error is needlessly brittle.
    resp = None
    for attempt in range(2):
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code < 300:
                break
            # Only a retryable status is worth re-reading. A 403 (billing gating
            # this route behind a role the licences SA lacks) answers the same way
            # a second later; the outcome is "unknown" either way, so the sleep
            # buys nothing and costs a second PER TENANT in the sweep.
            if attempt == 0 and _is_retryable_status(resp.status_code):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Could not read billing budgets for tenant %s: billing returned %d — "
                "coverage UNKNOWN, no budget will be created for it this sweep",
                tenant_id,
                resp.status_code,
            )
            return None
        except httpx.HTTPError as exc:
            if attempt == 0 and _is_retryable_transport_error(exc):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Could not read billing budgets for tenant %s — coverage UNKNOWN, "
                "no budget will be created for it this sweep",
                tenant_id,
                exc_info=True,
            )
            return None
    if resp is None:
        return None
    try:
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        covered: dict[str, int | None] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("source") != "license_sync" or not row.get("active"):
                continue
            row_license_id = row.get("license_id")
            if not row_license_id:
                continue
            key = str(row_license_id)
            value = row.get("budget_tokens")
            if row.get("period", "monthly") == "monthly" and value is not None:
                covered[key] = int(value)
            else:
                covered.setdefault(key, None)
        return covered
    except Exception:
        logger.warning(
            "Could not parse billing's budget list for tenant %s — coverage UNKNOWN",
            tenant_id,
            exc_info=True,
        )
        return None


# Tri-state for the quota read. "unknown" is NOT "absent" and the two must never
# collapse: treating an unanswered read as absent is what turns a fail-closed
# guard back into the unconditional overwrite it replaced.
QuotaConfigState = Literal["present", "absent", "unknown"]

# WHY an "unknown" carries a reason, added 2026-09-04. THREE distinct causes
# collapsed into one indistinguishable ``("unknown", None)``, and the CronJob
# reported the lot as a bare count. Measured: `licenses-metering-reconcile`
# failed 2026-09-04 03:41Z, BOTH attempts, exit=2, three alerts firing, and its
# entire operator-facing output was::
#
#     scanned=157 repaired(converged)=156 failed=1 unknown_quotas=1
#     METERING_PROVISIONING_FAILED license=019edca0-… tenant=019edc1e-…
#       budget_ok=True quota_ok=False
#
# The actual cause took ~30 minutes of Loki archaeology to recover: billing
# answered `GET /api/quota/check` with 404, and billing's ONLY 404 path is the
# foreign-owner rule at `billing_service/src/routers/quota.py:132`
# (``config.tenant_id != x-tenant-id``). So a 404 does not mean "no row" — it
# PROVES a row exists under a different tenant, i.e. the licence's tenant of
# record has diverged between the licences and billing databases. That is a
# named, recurring shape with a known remediation, and it deserved its own
# signal instead of sharing one with "billing's TCP connection reset".
#
# ⚠️ THE REASON IS OBSERVABILITY ONLY. It changes no decision: every reason
# below is still ``unknown``, and ``unknown`` still pushes NOTHING. See
# ``_fetch_quota_config_monthly``'s 404 note for why that must stay true.
QUOTA_UNKNOWN_REASONS: tuple[str, ...] = (
    # 404 — billing holds a row we are not the owner of. Tenant divergence.
    "foreign_tenant",
    # Any other non-retryable status, spelled ``http_<status>`` (e.g. http_500).
    "http_<status>",
    # httpx transport failure that survived the one bounded retry.
    "transport",
    # The loop ended with no response object at all.
    "no_response",
    # Billing answered, in a shape this cannot read.
    "unreadable_payload",
)


async def _fetch_quota_config_monthly(
    client: Any,
    *,
    license_id: str,
    plan_id: str,
    headers: dict[str, str],
) -> tuple[QuotaConfigState, int | None, str | None]:
    """What billing currently holds in ``request_quota_config`` for one licence.

    THE COUNTERPART TO ``_fetch_license_sync_budget_ids``, and it did not exist
    until 2026-08-24. Without it the reconcile sweep pushed
    ``plan.quota_monthly`` UNCONDITIONALLY on every run, so any quota an operator
    tuned by hand in billing was reverted by the next night's CronJob — silently,
    at HTTP 201, and counted as a success. Measured in the prod billing database
    on 2026-08-24: ``request_quota_config`` held 181 rows of which 155 had been
    REWRITTEN in the preceding 25 hours (newest ``updated_at``
    2026-08-24 03:41:56, the 03:41 CronJob), and 155 is exactly the count of
    active licences. Every row the sweep touched, every night.

    Returns ``(state, quota_monthly, unknown_reason)``:

    * ``("present", n, None)``   — billing holds a durable row, capped at ``n``.
    * ``("absent", None, None)`` — billing holds no row; this licence owes one.
    * ``("unknown", None, reason)`` — billing did not answer, or answered in a
      shape this cannot read. NOTHING may be pushed on an unknown: the whole
      point of the read is to avoid writing over a value we cannot see.

    ``unknown_reason`` is non-``None`` on exactly the ``unknown`` state and is
    one of ``QUOTA_UNKNOWN_REASONS`` (``http_<status>`` interpolated). It was
    added 2026-09-04 and it is OBSERVABILITY ONLY — it is carried into the
    sweep's summary as a counted breakdown and changes no decision here. The
    third element exists because the 2026-09-04 03:41Z CronJob failure reported
    ``unknown_quotas=1`` and nothing else, and telling ``foreign_tenant`` (a
    tenant-of-record divergence, with a known remediation) apart from
    ``transport`` (retry it) needed half an hour of Loki archaeology. See
    ``QUOTA_UNKNOWN_REASONS``.

    READ MECHANICS. Billing exposes no licence-keyed quota LIST, so this uses
    ``GET /api/quota/check?license_id=…&plan_id=…`` — the durable row is the
    source of truth there (``routers/quota.py::quota_check``: ``limit =
    config.quota_monthly if config is not None else quota_monthly``) and the
    response's ``quota_monthly`` is therefore the row's value, or ``None`` when
    no row exists.

    ⚠️ ``quota_monthly`` IS DELIBERATELY NOT SENT AS A QUERY PARAM. Billing falls
    back to that param when it finds no row, so sending the plan default would
    echo it back and make "absent" indistinguishable from "present and equal to
    the plan" — the read would report full coverage over an empty table and the
    sweep would create nothing, ever.

    ⚠️ A 404 IS ``unknown``, NOT ``absent``. ``quota_check`` 404s a row whose
    ``tenant_id`` differs from the ``X-Tenant-Id`` we send (billing's
    foreign-resource rule, ``billing_service/src/routers/quota.py:132`` — the
    ONLY path in that route that can 404), which is the same stale-legacy-tenant
    shape the budget leg documents. A row we are not allowed to see is still a
    row, and pushing over it is the exact harm this function exists to stop.
    Because that rule is billing's only 404, the status is not merely consistent
    with tenant divergence — it PROVES a row exists under another tenant, which
    is why it gets its own reason (``foreign_tenant``) rather than sharing
    ``http_<status>`` with the codes that mean nothing in particular.

    Gated like ``_fetch_license_sync_budget_ids``: one bounded retry, and only
    for a retryable status/transport error — a 403 answers identically a second
    later and the outcome is "unknown" either way. Unlike the budget read this is
    per-LICENCE, not per-tenant, because billing offers no batch form.
    """
    import httpx

    url = f"{METERING_SERVICE_URL}/api/quota/check"
    params = {"license_id": license_id, "plan_id": plan_id}
    resp = None
    for attempt in range(2):
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code < 300:
                break
            if attempt == 0 and _is_retryable_status(resp.status_code):
                await asyncio.sleep(1)
                continue
            if resp.status_code == 404:
                # NAMED SHAPE, not a generic HTTP error. billing's quota_check
                # has exactly one 404 (routers/quota.py:132, the foreign-owner
                # rule), so this status is positive evidence that a row EXISTS
                # under a tenant that is not the one we sent.
                logger.warning(
                    "Could not read billing's quota config for license %s: billing returned "
                    "404 — the configured quota is UNKNOWN, so nothing will be pushed for it "
                    "this sweep. LIKELY CAUSE: billing 404s a request_quota_config row whose "
                    "tenant_id differs from the X-Tenant-Id we sent (its foreign-owner rule is "
                    "the only 404 that route has), so this licence's TENANT OF RECORD has "
                    "probably diverged between the two databases. CHECK: compare "
                    "licenses.license.tenant_id against billing.request_quota_config.tenant_id "
                    "for license_id=%s; we sent X-Tenant-Id=%s. Correct the divergent row "
                    "rather than forcing a push — the push would overwrite a quota we cannot "
                    "see.",
                    license_id,
                    license_id,
                    headers.get("X-Tenant-Id"),
                )
                return ("unknown", None, "foreign_tenant")
            logger.warning(
                "Could not read billing's quota config for license %s: billing returned %d — "
                "the configured quota is UNKNOWN, so nothing will be pushed for it this sweep",
                license_id,
                resp.status_code,
            )
            return ("unknown", None, f"http_{resp.status_code}")
        except httpx.HTTPError as exc:
            if attempt == 0 and _is_retryable_transport_error(exc):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Could not read billing's quota config for license %s — the configured "
                "quota is UNKNOWN, so nothing will be pushed for it this sweep",
                license_id,
                exc_info=True,
            )
            return ("unknown", None, "transport")
    if resp is None:
        return ("unknown", None, "no_response")
    try:
        payload = resp.json()
        row = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(row, dict):
            raise ValueError("quota check payload is not an object")
        # `quota_monthly` missing and `quota_monthly: null` mean the same thing
        # here — billing found no durable row — because we sent no fallback.
        current = row.get("quota_monthly")
        if current is None:
            return ("absent", None, None)
        return ("present", int(current), None)
    except Exception:
        logger.warning(
            "Could not parse billing's quota check for license %s — the configured quota "
            "is UNKNOWN, so nothing will be pushed for it this sweep",
            license_id,
            exc_info=True,
        )
        return ("unknown", None, "unreadable_payload")


async def _push_token_budget(
    client: Any,
    *,
    license_id: str,
    tenant_id: str,
    budget_tokens: int,
    headers: dict[str, str],
) -> bool:
    """POST billing ``/api/budget`` with one retry. True iff billing accepted it.

    ⚠️ CHANGED 2026-10-15 — the warning below is superseded for the licences
    side: the licence's effective budget (override, else plan) IS the cap, so
    this upsert is now also sent on purpose when that budget changes
    (``push_effective_token_budgets``) and when the reconcile sweep finds billing
    holding a different value. A cap hand-set in billing but not recorded as the
    licence's ``token_budget_override`` is overwritten by design; record it on the
    licence. The billing-side facts below (licence-keyed upsert, consumed_tokens
    preserved) are what make that safe.

    ⚠️ CALL ONLY WHEN BILLING IS KNOWN NOT TO HOLD A BUDGET FOR THIS LICENCE —
    because this push OVERWRITES, not because it duplicates. The create path is
    safe because the licence is brand new and billing can hold nothing for it;
    the reconcile sweep is safe because it reads
    ``GET /api/budget/list/{tenant}`` first — see
    ``_fetch_license_sync_budget_ids``.

    ⚠️ RETRACTED 2026-08-10 — this docstring used to assert that billing is "NOT
    idempotent" and that a second POST "creates a DUPLICATE row", citing "191
    rows for 186 distinct licences", and that claim was the stated reason the
    reconciler repaired quota only. It was a misattribution, and it kept a
    94-licence hole open. Measured in the prod billing database:

        source        rows   distinct license_id
        license_sync   185                   185
        manual          10                     8

    The duplication is entirely ``source='manual'`` rows, which stack BY DESIGN
    (top-ups). ``upsert_license_sync_budget`` — verified present in the DEPLOYED
    billing pod, not just its repo — is a real read-before-write upsert, so
    ``source='license_sync'`` genuinely IS an idempotency key for this push.

    ⚠️ CORRECTED 2026-08-11 — two further claims that stood here were FALSE, and
    the truth is worse for this caller, not better. They were: (a) "no unique
    constraint on ``license_id``, so nothing in the schema stops a second row",
    and (b) "billing's upsert keys on ``tenant_id`` too, [so] prod's stale
    legacy tenants mean 'billing will dedup it' is not a guarantee". Read
    straight out of billing's source:

    * ``billing_service/src/services/budget_crud.py``
      ``_select_active_license_sync_budget`` selects on
      ``(license_id, period, source='license_sync', active)``. ``tenant_id`` is
      deliberately ABSENT from that key.
    * alembic ``0010`` declares ``CREATE UNIQUE INDEX
      uq_token_budgets_license_sync_active ON token_budgets (license_id, period)
      WHERE source='license_sync' AND active`` — again no ``tenant_id``. So the
      schema DOES stop a second active license_sync row, and a stale legacy
      tenant does NOT make billing miss the existing one.

    What a redundant POST actually does: billing finds the row and MUTATES it.
    ``budget_crud._apply()`` re-tenants it in place
    (``row.tenant_id = tenant_id; row.tenant_name = tenant_id``) and then sets
    ``row.budget_tokens`` to whatever this push sends — the PLAN DEFAULT. Any
    operator-set cap on that licence is silently reset to the plan value.
    ``consumed_tokens`` is preserved, so nothing in usage reporting reveals it.

    Billing's behaviour is deliberate and right on its own terms ("The licence
    moved tenants upstream. One licence, one tenant.") — the defect was this
    repo's description of it, and no billing change is implied. Two caveats,
    stated so nobody re-derives them: billing's own ``routers/budget.py``
    docstring still describes the key as including ``tenant_id`` while its code
    does not; and the index above is read from alembic ``0010`` in billing's
    repo — it was NOT re-verified against the live billing database on
    2026-08-11 (the read was not available in that session). The overwrite
    hazard does not depend on the index either way: the SELECT alone is enough.
    """
    import httpx

    for attempt in range(2):
        try:
            resp = await client.post(
                f"{METERING_SERVICE_URL}/api/budget",
                json={
                    "tenant_id": tenant_id,
                    "license_id": license_id,
                    "budget_tokens": budget_tokens,
                    "period": "monthly",
                    # source='license_sync' makes the metering POST idempotent on
                    # (tenant, license, period). Without this, the periodic
                    # sync-from-licenses pull and the auto-provision push each
                    # create their own row and the cascade-drain stacks them,
                    # doubling the effective entitlement. See live E2E bug B2 /
                    # 179 historical duplicates.
                    "source": "license_sync",
                },
                headers=headers,
            )
            # httpx does not raise on 4xx/5xx — a non-2xx (e.g. a 503 while
            # metering rolls out, or a 401 on a bad SA token) must be treated as a
            # FAILURE, not silently accepted as done.
            if resp.status_code < 300:
                return True
            # ...but only a RETRYABLE non-2xx is worth re-sending. See
            # `_is_retryable_status`: a 403/422 is a VERDICT, and no amount of
            # sleeping turns a permission error into a 201. This helper was left
            # on the old retry-anything shape when the offering helpers were
            # fixed; the reconcile sweep calls it inline PER LICENCE with a route
            # ceiling of `le=5000`, so a billing 403 cost a wasted second and a
            # doomed second POST for every licence in the sweep.
            if attempt == 0 and _is_retryable_status(resp.status_code):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Failed to provision token budget for license %s: metering returned %d: %s",
                license_id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            if attempt == 0 and _is_retryable_transport_error(exc):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Failed to provision token budget for license %s after retry",
                license_id,
                exc_info=True,
            )
            # RETURN, do not fall through. Without this, declining the retry on a
            # deterministic transport error simply drops out of the `except` and
            # the `for` loop runs attempt 1 anyway — the retry we just refused.
            # Caught by TestATransportVerdictIsNotRetried, which measured 2 calls.
            return False
    return False


def _app_name_for(app_id: Any) -> str | None:
    """The app's display name, or ``None`` if it cannot be read.

    Billing stores ``app_name`` beside ``app_id`` on ``request_quota_config`` and
    the resolver groups candidates by ``(tenant_id, app_name)``, so the name is not
    decoration — it is half the grouping key.

    ⛔ Returns ``None`` on ANY failure rather than raising. This is called on the
    licence-creation hot path inside a fire-and-forget provisioning task; a lookup
    that raised would abort the whole provisioning push and lose the budget too, to
    save a display string. A ``None`` name still writes a usable row: ``app_id`` is
    what the resolver's ``is_not(None)`` predicate keys on, and billing COALESCEs a
    null rather than clobbering, so a later sync repairs the name.
    """
    if app_id is None:
        return None
    try:
        app = db_manager.get_app(UUID(str(app_id)))
    except Exception:
        logger.warning("Could not read app name for app_id=%s; writing app_id alone", app_id)
        return None
    return str(app.name) if app is not None else None


async def _push_quota_config(
    client: Any,
    *,
    license_id: str,
    tenant_id: str,
    quota_monthly: int,
    headers: dict[str, str],
    app_id: str | None = None,
    app_name: str | None = None,
) -> bool:
    """POST billing ``/api/quota/config`` with one retry. True iff accepted.

    ⚠️ CALL ONLY WHEN BILLING IS KNOWN TO HOLD NO ROW FOR THIS LICENCE, or when
    an operator has explicitly asked for the plan value to be forced over
    whatever is there — see ``_fetch_quota_config_monthly`` and
    ``run_metering_reconcile(force_plan_quota=…)``. This push OVERWRITES.

    ⚠️ CORRECTED 2026-08-24. This docstring used to say the push is "SAFE to
    re-push" because ``billing.request_quota_config`` has
    ``PRIMARY KEY (license_id)``, "so repeated pushes leave at most one row per
    licence", and cited that as the reason the reconciler repaired quota and
    nothing else. Both halves of the sentence are true and neither addresses the
    risk: the primary key bounds the ROW COUNT, and the harm is to the VALUE.
    ``upsert_request_quota_config`` finds the existing row and sets
    ``quota_monthly`` to whatever this body carries — the plan default, when the
    caller is the sweep.

    What that cost, measured in the prod billing database on 2026-08-24:
    ``request_quota_config`` held 181 rows, 155 of them rewritten in the previous
    25 hours with a newest ``updated_at`` of 2026-08-24 03:41:56 — the ``41 3 * *
    *`` reconcile CronJob, which runs ``RECONCILE_DRY_RUN=false``
    ``RECONCILE_REVALIDATE=true``. 155 is the count of active licences. So every
    hand-tuned quota an operator set during the day was reverted to the plan
    default before morning, at HTTP 201, counted in ``quotas_pushed`` as work
    done. The reconcile loop is therefore now read-before-write, exactly like the
    budget leg it was once contrasted against.

    The create path (``_provision_budget``) stays unconditional and is safe on
    its own terms: the licence is brand new, so billing can hold nothing for it.
    """
    import httpx

    for attempt in range(2):
        try:
            resp = await client.post(
                f"{METERING_SERVICE_URL}/api/quota/config",
                json={
                    # tenant_id is REQUIRED by billing's QuotaConfigRequest
                    # (min_length=1) — see the header comment in
                    # _provision_budget. FastAPI never populates a body model
                    # from a header, so a header-only tenant is a deterministic
                    # 422 and leaves no config row.
                    #
                    # ⚠️ This used to end "...that burns BOTH retries". Not any
                    # more: the retry below is gated on `_is_retryable_status`,
                    # so a 422 costs exactly one attempt. Corrected rather than
                    # left, because it sat twelve lines above the code that
                    # falsifies it.
                    "tenant_id": tenant_id,
                    "license_id": license_id,
                    "quota_monthly": quota_monthly,
                    # ⭐ WITHOUT app_id BILLING'S RESOLVER CANNOT SEE THIS ROW.
                    # `license_resolver._build_map` selects candidates with
                    # `where(RequestQuotaConfig.app_id.is_not(None))`, so a row we
                    # write with a NULL app is permanently unresolvable and every
                    # cost event for that tenant stays unattributed. Measured in
                    # prod 2026-08-28, BEFORE this line existed: 26 of 189 rows had
                    # a NULL app_id, because the only writer that ever set one is
                    # the operator-triggered `/api/budget/sync-from-licenses` while
                    # THIS push is the one the nightly CronJob actually makes.
                    #
                    # `None` is sent rather than omitted, and that is SAFE: billing's
                    # `upsert_request_quota_config` COALESCEs
                    # (`func.coalesce(excluded.app_id, RequestQuotaConfig.app_id)`),
                    # so a null cannot clobber a stored value — it only declines to
                    # set one. Pinned from billing's side by
                    # `test_omitted_app_id_does_not_clobber_a_stored_one`.
                    "app_id": app_id,
                    "app_name": app_name,
                },
                headers=headers,
            )
            # See _push_token_budget: a non-2xx is a failure to log, not a silent
            # success — and only a RETRYABLE one is worth re-sending.
            #
            # The comment above ("a deterministic 422 that burns both retries")
            # described this defect in place and did not fix it: a 422 is a
            # verdict about the BODY, so the second POST sends the identical body
            # and gets the identical 422, one wasted second later.
            if resp.status_code < 300:
                return True
            if attempt == 0 and _is_retryable_status(resp.status_code):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Failed to provision request quota for license %s: metering returned %d: %s",
                license_id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            if attempt == 0 and _is_retryable_transport_error(exc):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Failed to provision request quota for license %s after retry",
                license_id,
                exc_info=True,
            )
            # RETURN, do not fall through. Without this, declining the retry on a
            # deterministic transport error simply drops out of the `except` and
            # the `for` loop runs attempt 1 anyway — the retry we just refused.
            # Caught by TestATransportVerdictIsNotRetried, which measured 2 calls.
            return False
    return False


async def _provision_budget(
    license_id: str,
    tenant_id: str,
    plan_id: str,
    app_id: str | None = None,
    *,
    token_budget_override: int | None = None,
    quota_override: int | None = None,
) -> bool:
    """Fire-and-forget: create budget and quota config in metering service.

    ``token_budget_override`` / ``quota_override`` (#239, operator provisioning
    only): when given and ``>= 0`` they replace the PLAN's value for THIS
    licence's push only — the ``Plan`` row is never modified. ``None`` (the
    default, and what every HTTP caller passes) is the plan-driven behaviour
    exactly. A negative override is ignored (plan value wins) as defence in
    depth; the operator scripts reject it before calling. The sentinel-tenant
    budget skip still applies to an overridden budget.

    Returns True iff every push the plan owes was accepted AND
    ``metering_provisioned_at`` was stamped; False on every other path (it still
    never raises). The HTTP create route discards the result; the operator
    scripts that create licences outside that route (``provision_tenant_bundle.py``,
    ``migrate_member_licences_to_tenant.py``) call it through
    ``provision_license_metering`` and report a False.

    Called after license creation. Failures never block the license-creation
    response — but they are no longer INVISIBLE. Two things changed after the
    2026-08-06 leak (see METERING_FAILURE_MARKER and migration 018):

    * every failure path logs at ERROR with ``METERING_PROVISIONING_FAILED``;
    * ``license.metering_provisioned_at`` is stamped ONLY when every push the
      plan owes has been accepted, so an unmetered licence is queryable from
      the licences database alone instead of needing a cross-database diff.

    A plan that owes nothing (NULL quota AND NULL budget = unlimited) counts as
    complete and is stamped: that keeps ``metering_provisioned_at IS NULL`` a
    clean "unfinished" signal rather than a mix of "failed" and "nothing to do".

    Outbound calls carry a Keycloak service-account JWT. billing-service gates
    the budget/quota provisioning routes with ``require_internal``, which accepts
    ``billing-internal``, ``onboarding-internal`` or ``license-admin``.

    ⚠️ CORRECTED 2026-08-10: this used to name the ``metering-internal`` realm
    role, retired in billing on 2026-07-16 and present in none of its role sets.

    ⚠️ THE DB SESSION IS CLOSED BEFORE ANY HTTP CALL, deliberately. This used to
    hold the ``get_session()`` open across ``_metering_auth_headers`` and the
    whole ``httpx.AsyncClient`` block. ``SessionLocal`` is ``autocommit=False``,
    so the ``Plan`` SELECT opens a transaction and pgbouncer (TRANSACTION
    pooling) pins a server backend for as long as that transaction is open —
    here, for the duration of up to three network calls: a cold-cache Keycloak
    token fetch (``service_auth._TOKEN_FETCH_TIMEOUT_SECONDS = 5.0``) plus the
    budget and quota pushes at ``timeout=5.0`` each WITH a bounded retry, i.e.
    ~25s worst case, not the ~10s the two pushes suggest on their own. A
    provisioning burst therefore exhausts the pooler, and because the app engine
    is ``NullPool`` the failure surfaces on the AUTH path as a connect error
    with no local pool to absorb it.

    The ``Plan`` row is read-only here and only two scalars are used, so they are
    snapshotted into locals and the session is released. Ordering is pinned by
    ``tests/unit/test_metering_durability.py``
    ``TestSessionIsNotHeldAcrossTheHttpPushes``.

    ⚠️ AND THE SQL RUNS OFF THE EVENT LOOP (closed 2026-08-16). Releasing the
    session early fixed how LONG a pgbouncer backend was pinned; it did not stop
    the statements themselves executing on the loop thread. "Fire-and-forget" here
    is ``_spawn_background_task`` -> ``asyncio.create_task``, i.e. a task on THE
    SAME loop — so the plan SELECT and the provisioning stamp froze every
    in-flight request on the pod, including ``/license/introspect``. Both now go
    through ``asyncio.to_thread``, the idiom the flipped handlers already use.
    A second, quieter consequence: connections opened on the loop thread belong
    to neither pool the governor sizes (``HANDLER_THREAD_LIMIT`` +
    ``TO_THREAD_EXECUTOR_LIMIT``), so they escaped the 16-per-pod ceiling that
    exists to keep this service inside its 40-connection pgbouncer budget.
    Pinned by ``tests/unit/test_no_sql_on_event_loop.py``.
    """
    import httpx

    def _read_plan_budgets() -> tuple[Any, Any, str | None] | None:
        """Snapshot the plan's two budget scalars, or None when the row is gone.

        A plain ``def`` handed to a worker thread, so connect / SELECT / close all
        happen off the event loop and on ONE thread.

        ⚠️ get_session() is INSIDE this function's try on purpose: under NullPool
        it opens a real connection and can raise. Outside, that failure escaped
        ``_provision_budget``'s swallow entirely and propagated into
        ``_background_task_done``.

        ⚠️ The inner finally owns the session's whole lifetime. Do NOT hoist it to
        a trailing ``finally: session.close()`` — if get_session() raises,
        ``session`` is unbound there and the close raises UnboundLocalError,
        masking the real exception.
        """
        session = db_manager.get_session()
        try:
            plan = session.query(Plan).filter(Plan.id == UUID(plan_id)).first()
            if not plan:
                return None
            # #239 / migration 041: the licence's OWN stored overrides beat the
            # plan. Read in the same session, before close. A licence row that is
            # absent (a test seeding a plan only) simply has no overrides.
            overrides = (
                session.query(License.token_budget_override, License.quota_override)
                .filter(License.id == UUID(license_id))
                .first()
            )
            stored_budget, stored_quota = overrides if overrides is not None else (None, None)
            # Migration 043: a MONEY tenant (budget_currency set) owes billing no
            # token budget. Read in the same session; a tenant row that is absent
            # reads as NULL, i.e. the unchanged token-budget behaviour.
            currency = (
                session.query(Tenant.budget_currency)
                .filter(Tenant.id == UUID(str(tenant_id)))
                .scalar()
            )
            # Snapshot before close: these are plain ints/None at runtime, so
            # nothing below re-touches the (now detached) ORM instance.
            # cast: SQLAlchemy Column[int] at the type level, int | None at runtime.
            budget, quota = effective_metering_values(
                stored_budget,
                stored_quota,
                cast("int | None", plan.token_budget_monthly),
                cast("int | None", plan.quota_monthly),
            )
            return budget, quota, cast("str | None", currency)
        finally:
            session.close()

    try:
        # to_thread: this coroutine is spawned by POST /licenses/user/{subject_id}
        # via _spawn_background_task, which is asyncio.create_task — "background"
        # there means THE SAME EVENT LOOP, not off it. So this blocking SELECT (a
        # fresh physical connection under NullPool) stalled /license/introspect
        # and every other in-flight request on the pod, which is exactly the
        # head-of-line blocking that turns a cold-cache introspect into a 403.
        budgets = await asyncio.to_thread(_read_plan_budgets)
        if budgets is None:
            # Not a benign early return: the licence exists and nothing will
            # ever meter it. Loud, with the same marker as every other failure.
            logger.error(
                "%s license=%s tenant=%s plan=%s — plan row not found, nothing provisioned; "
                "repair with POST %s",
                METERING_FAILURE_MARKER,
                license_id,
                tenant_id,
                plan_id,
                METERING_RECONCILE_PATH,
            )
            return False
        # Already the EFFECTIVE values: the licence's stored override (041) where
        # set, else the plan's (models.effective_metering_values).
        effective_token_budget, effective_quota, budget_currency = budgets
        # #239: an explicit override ARGUMENT beats even the stored one (an
        # operator script passing the value it just wrote). None = stored/plan.
        if token_budget_override is not None and token_budget_override >= 0:
            effective_token_budget = token_budget_override
        if quota_override is not None and quota_override >= 0:
            effective_quota = quota_override

        headers = await _metering_auth_headers(tenant_id, license_id)

        # Both default True so a plan that owes a given push nothing cannot be
        # reported as having failed it.
        budget_ok = True
        quota_ok = True

        async with httpx.AsyncClient(timeout=5.0) as client:
            # Provision token budget if the plan sets one. token_budget_monthly=0
            # is a BLOCK-ALL (zero-allowance) budget and MUST be forwarded — the
            # old ``budget and budget > 0`` guard treated the falsy 0 as "no
            # budget" and skipped the push, so a block-all license silently
            # behaved as unlimited. NULL means unlimited (correctly skipped).
            # Identical to the quota guard below; the two got out of step.
            # Negatives are rejected upstream at PlanCreate/PlanUpdate (Field
            # ge=0, same as quota_monthly); the >= 0 here is defence in depth
            # against a legacy negative row. Without that upstream floor this
            # very guard would SKIP a negative budget and hand back an
            # effectively unlimited licence.
            # NOTE (cross-repo): the companion billing change HAS landed —
            # billing #73 relaxed ``TokenBudgetCreate.budget_tokens`` from
            # ``Field(gt=0)`` to ``ge=0``, matching what it already did for
            # quota_monthly. Verified against the DEPLOYED billing pod, not just
            # its repo: the served OpenAPI declares
            # ``{"type": "integer", "minimum": 0.0}``. So a block-all budget is
            # now representable end to end and this push no longer 422s.
            # Pinned from this side by tests/unit/test_billing_contract.py
            # ``test_zero_budget_is_accepted_because_block_all_must_be_representable``.
            if (
                effective_token_budget is not None
                and effective_token_budget >= 0
                and budget_currency is not None
            ):
                # Migration 043 / PLAN §2.2: this tenant's budget is MONEY in
                # billing. A license_sync token budget would be a second cap
                # (billing pools them per tenant), so none is pushed — overrides
                # included. The quota below is still owed; the licence counts as
                # provisioned once billing accepts it.
                logger.info(
                    "token_budget_skipped_money_tenant license=%s tenant=%s "
                    "budget_currency=%s — no token budget pushed, quota only",
                    license_id,
                    tenant_id,
                    budget_currency,
                )
            elif (
                effective_token_budget is not None
                and effective_token_budget >= 0
                and str(tenant_id) in BILLING_SENTINEL_TENANT_IDS
            ):
                # #234: billing refuses budgets for its sentinel tenant BY
                # DESIGN (422 "not a registered tenant"). Pushing would only
                # manufacture a METERING_PROVISIONING_FAILED. Quota still goes.
                logger.info(
                    "Metering: skipping token budget for license=%s tenant=%s — sentinel "
                    "tenant, billing refuses budgets by design (#234)",
                    license_id,
                    tenant_id,
                )
            elif effective_token_budget is not None and effective_token_budget >= 0:
                budget_ok = await _push_token_budget(
                    client,
                    license_id=license_id,
                    tenant_id=tenant_id,
                    # cast: SQLAlchemy Column[int] at the type level, plain int
                    # at runtime — the guard above already excluded NULL.
                    budget_tokens=cast(int, effective_token_budget),
                    headers=headers,
                )

            # Provision request quota if the plan sets one. quota_monthly=0 is a
            # BLOCK-ALL (zero-allowance) plan and MUST be forwarded — the old
            # ``quota and quota > 0`` guard treated the falsy 0 as "no quota" and
            # skipped the push, so a block-all license silently behaved as
            # unlimited. NULL means unlimited (correctly skipped). Negatives are
            # rejected upstream at PlanCreate/PlanUpdate (Field ge=0); the >= 0
            # here is defence in depth against a legacy negative row.
            if effective_quota is not None and effective_quota >= 0:
                quota_ok = await _push_quota_config(
                    client,
                    license_id=license_id,
                    tenant_id=tenant_id,
                    # cast: see the budget push above.
                    quota_monthly=cast(int, effective_quota),
                    headers=headers,
                    app_id=app_id,
                    # to_thread: ``db_manager.*`` is blocking SQL and this coroutine
                    # runs on the shared event loop, so an inline call here is
                    # head-of-line blocking for every other request on the pod.
                    app_name=(
                        await asyncio.to_thread(_app_name_for, app_id)
                        if app_id is not None
                        else None
                    ),
                )

        if budget_ok and quota_ok:
            # to_thread: same reasoning as the plan read above — UPDATE + COMMIT
            # on a fresh NullPool connection, on the request loop.
            return await asyncio.to_thread(_mark_metering_provisioned, license_id)
        else:
            # The licence is live and (partly) unmetered. Do NOT stamp: a stamp
            # written on a half-success would hide exactly the gap that cost us
            # licence 019fd7a0-888f-749e-86c2-3b935a196ebf.
            logger.error(
                "%s license=%s tenant=%s plan=%s budget_ok=%s quota_ok=%s — the licence is "
                "LIVE but not fully metered; repair with POST %s",
                METERING_FAILURE_MARKER,
                license_id,
                tenant_id,
                plan_id,
                budget_ok,
                quota_ok,
                METERING_RECONCILE_PATH,
            )
            return False
    # Top-level fire-and-forget: any unexpected error is logged and swallowed —
    # this background task must never affect the license-creation response. It
    # is still an ERROR: reaching here means the licence is unmetered, and no
    # stamp is written, so the reconciler will pick it up.
    except Exception:
        logger.error(
            "%s license=%s tenant=%s plan=%s — provisioning raised; repair with POST %s",
            METERING_FAILURE_MARKER,
            license_id,
            tenant_id,
            plan_id,
            METERING_RECONCILE_PATH,
            exc_info=True,
        )
        return False


async def provision_license_metering(
    license_id: str,
    tenant_id: str,
    plan_id: str,
    app_id: str | None = None,
    *,
    token_budget_override: int | None = None,
    quota_override: int | None = None,
) -> bool:
    """THE one metering-provisioning path for a NEWLY CREATED licence.

    ``token_budget_override`` / ``quota_override``: per-licence values that
    replace the plan's for this push only (see ``_provision_budget``). ``None``
    keeps the plan value.

    Shared by ``POST /licenses/user/{subject_id}`` (which spawns
    ``_provision_budget`` fire-and-forget) and by every operator script that
    creates a licence with a direct ``db.create_license`` — those used to skip
    this step entirely, which is the 2026-08-06 unmetered-licence leak shape.

    An EXISTING licence whose token budget changes goes through
    ``push_effective_token_budgets`` (2026-10-15); its quota does not.

    ⚠️ NEW licences only: the budget and quota pushes OVERWRITE (see
    ``_push_token_budget``), which is safe only because billing can hold nothing
    for a licence created a moment ago. An existing licence goes through the
    read-before-write reconcile sweep instead (``run_metering_reconcile``).

    Returns True iff provisioned and stamped. A False leaves
    ``metering_provisioned_at`` NULL and ``metering_verified_at`` NULL; the daily
    ``licenses-metering-reconcile`` CronJob selects every live budget-owing
    licence (``RECONCILE_REVALIDATE=true``) and repairs it read-before-write.
    Resolved through the module global so tests patching ``_provision_budget``
    are seen here.
    """
    if token_budget_override is None and quota_override is None:
        # Byte-identical call shape for every pre-#239 caller, so tests that patch
        # ``_provision_budget`` with a 4-arg fake keep working.
        return await _provision_budget(
            license_id=license_id, tenant_id=tenant_id, plan_id=plan_id, app_id=app_id
        )
    return await _provision_budget(
        license_id=license_id,
        tenant_id=tenant_id,
        plan_id=plan_id,
        app_id=app_id,
        token_budget_override=token_budget_override,
        quota_override=quota_override,
    )


class TokenBudgetRepushResult(BaseModel):
    """Outcome of ``push_effective_token_budgets`` (one entry per examined licence)."""

    pushed: list[str] = []
    torn_down: list[str] = []
    failed: list[str] = []
    skipped_money_tenant: list[str] = []
    skipped_sentinel: list[str] = []


async def push_effective_token_budgets(
    *,
    plan_id: str | None = None,
    license_ids: list[str] | None = None,
    plan_driven_only: bool = False,
    reason: str,
) -> TokenBudgetRepushResult:
    """Push each LIVE licence's EFFECTIVE token budget to billing, AFTER a change.

    THE UPDATE-TIME COUNTERPART to ``provision_license_metering`` (2026-10-15).
    Until this existed a budget reached billing only when a licence was created:
    ``PATCH /plans/{id}`` changing ``token_budget_monthly`` pushed nothing, the
    nightly reconcile only created MISSING budgets, and the ingress (license-authz
    reads the Redis limit only billing writes) kept refusing a raised KAG cap until
    billing was restarted.

    Selection: ``status='active'``, not soft-deleted, not past ``expires_at`` —
    revoked, archived (``deleted_at``) and expired licences are never pushed — narrowed by ``plan_id`` and/or
    ``license_ids``. ``plan_driven_only`` additionally drops licences carrying a
    ``token_budget_override``: their effective budget does not move with the plan.

    Per licence, the effective budget (``effective_metering_values``) is:

    * an int ``>= 0`` → ``_push_token_budget``. Billing's ``source='license_sync'``
      POST is an upsert keyed on the licence: it UPDATES ``budget_tokens``, keeps
      ``consumed_tokens`` and rewrites Redis immediately.
    * ``None`` (unlimited) → ``_teardown_token_budget``: a stale cap left active in
      billing would keep enforcing a limit the licence no longer has.
    * money tenant (migration 043) / sentinel tenant (#234) → skipped, exactly as
      ``_provision_budget`` skips them.

    Failure contract = the create path's: never raises, never fails the caller's
    request. A failed push is an ERROR with ``METERING_FAILURE_MARKER`` and the
    licence's ``metering_verified_at`` is cleared, so the reconcile sweep (which
    now updates a differing budget) selects and repairs it.
    """
    import httpx

    wanted: list[UUID] | None = (
        [UUID(str(one)) for one in license_ids] if license_ids is not None else None
    )

    def _select() -> list[Any]:
        session = db_manager.get_session()
        try:
            now = datetime.now(UTC)
            query = (
                session.query(
                    License.id,
                    License.tenant_id,
                    License.token_budget_override,
                    Plan.token_budget_monthly,
                    Tenant.budget_currency,
                )
                .join(Plan, Plan.id == License.plan_id)
                .outerjoin(Tenant, Tenant.id == License.tenant_id)
                .filter(License.status == "active")
                # Soft-deleted (archived) rows are retained for billing evidence
                # only; they are not entitlements and get nothing pushed.
                .filter(License.deleted_at.is_(None))
                .filter((License.expires_at.is_(None)) | (License.expires_at > now))
            )
            if plan_id is not None:
                query = query.filter(License.plan_id == UUID(str(plan_id)))
            if wanted is not None:
                query = query.filter(License.id.in_(wanted))
            if plan_driven_only:
                query = query.filter(License.token_budget_override.is_(None))
            return query.order_by(License.created_at).all()
        finally:
            session.close()

    result = TokenBudgetRepushResult()
    try:
        rows = await asyncio.to_thread(_select)
    except Exception:
        logger.error(
            "%s token budget re-push (%s) could not select licences — nothing pushed; "
            "repair with POST %s",
            METERING_FAILURE_MARKER,
            reason,
            METERING_RECONCILE_PATH,
            exc_info=True,
        )
        return result
    if not rows:
        return result

    async with httpx.AsyncClient(timeout=5.0) as client:
        for license_id, tenant_id, override, plan_budget, budget_currency in rows:
            lic = str(license_id)
            tenant = str(tenant_id)
            budget, _quota = effective_metering_values(override, None, plan_budget, None)
            try:
                if budget_currency is not None:
                    result.skipped_money_tenant.append(lic)
                    continue
                if tenant in BILLING_SENTINEL_TENANT_IDS:
                    result.skipped_sentinel.append(lic)
                    continue
                if budget is None or budget < 0:
                    ok = await _teardown_token_budget(lic, tenant)
                    if ok:
                        result.torn_down.append(lic)
                else:
                    headers = await _metering_auth_headers(tenant, lic)
                    ok = await _push_token_budget(
                        client,
                        license_id=lic,
                        tenant_id=tenant,
                        budget_tokens=int(budget),
                        headers=headers,
                    )
                    if ok:
                        result.pushed.append(lic)
            except Exception:
                logger.warning("token budget re-push raised for license %s", lic, exc_info=True)
                ok = False
            if not ok:
                result.failed.append(lic)
                logger.error(
                    "%s license=%s tenant=%s effective_token_budget=%s — token budget "
                    "change (%s) NOT applied in billing; repair with POST %s",
                    METERING_FAILURE_MARKER,
                    lic,
                    tenant,
                    budget,
                    reason,
                    METERING_RECONCILE_PATH,
                )
                await asyncio.to_thread(
                    _stamp_license_metering,
                    lic,
                    {License.metering_verified_at: None},
                    "metering_verified_at reset after a failed token budget push",
                )
    logger.info(
        "token budget re-push (%s): pushed=%d torn_down=%d failed=%d "
        "skipped_money_tenant=%d skipped_sentinel=%d",
        reason,
        len(result.pushed),
        len(result.torn_down),
        len(result.failed),
        len(result.skipped_money_tenant),
        len(result.skipped_sentinel),
    )
    return result


class MigrationError(RuntimeError):
    """A migration RAN and FAILED — a permanent fault, never retried into readiness.

    Distinct from "the database was unreachable" so ``lifespan`` can keep
    fail-fast for the case it is right for. It is not a connect fault, so
    ``is_db_unreachable()`` returns False for it and the ``raise`` re-fires.
    """


# Latched by _run_db_bootstrap when it finds a pristine database, cleared when the
# warning is emitted. Module scope rather than a local because the deferred-retry
# path calls _run_db_bootstrap AGAIN, and by then create_tables() may already have
# run — so the fact has to outlive the call that observed it.
_fresh_database_pending_warning = False


def _run_db_bootstrap() -> None:
    """Bring the schema up to date. BLOCKING — never call this on the event loop.

    Raises a connect-scoped fault (``is_db_unreachable()`` True) when the
    database cannot be reached, and ``MigrationError`` when a migration ran and
    failed. Split out of ``lifespan`` so the retry path re-runs byte-identical
    work: a retry that diverges from the boot path is a retry that can report
    ready on a schema the boot path would have rejected.

    Order is load-bearing and pinned by
    ``test_bootstrap_seed.py::test_run_db_bootstrap_invokes_the_seed``:
    probe -> create -> migrate -> seed.
    """
    # Ask FIRST, because after the next line the app-tables half is always False
    # and the question becomes unanswerable. See db_manager.database_is_pristine().
    # This is now the first database touch of the boot, so an unreachable database
    # raises HERE rather than from create_tables() — that is a connect fault either
    # way, so lifespan defers instead of exiting, and a test pins it.
    global _fresh_database_pending_warning
    if db_manager.database_is_pristine():
        _fresh_database_pending_warning = True
    # ORM create_all only makes MISSING tables; it never ALTERs an existing
    # table to add a column …
    db_manager.create_tables()
    if _fresh_database_pending_warning:
        # Emitted HERE — immediately after create_tables(), BEFORE the migration
        # runner — and latched, both for the same reason: this warning gets exactly
        # one chance in a database's life, and the naive placement threw it away.
        # If it sat after the migration step, a database blip there (the very fault
        # class this boot path exists for) would defer, the retry would re-run
        # _run_db_bootstrap byte-identically, and by then `license` exists — so the
        # probe reports "not pristine" and the warning is lost FOREVER on a database
        # that really was baselined. Reproduced before this was moved.
        #
        # The latch covers the remaining window: if create_tables() itself fails
        # part-way, the flag survives into the retry that finishes the job.
        #
        # WARNING, not INFO, and it fires at most once per process. It is loud
        # because this is the moment the schema stops being the migrations' and
        # becomes models.py's alone, and nothing else in the running system ever
        # says so.
        _fresh_database_pending_warning = False
        logger.warning(
            "db_bootstrap_fresh_database: this database was EMPTY at boot (no app "
            "tables, no migration history), so create_all() built the whole schema "
            "from models.py and every SQL migration will be BASELINED (stamped "
            "applied, never run). Anything a migration expresses that the ORM does "
            "not is absent here — data seeds, backfills, renames, and any "
            "index/CHECK/default not mirrored in models.py. "
            "tests/unit/test_schema_parity.py is the ONLY thing keeping the two "
            "equal; if it has been deleted or muted, this schema is unverified."
        )
    # … then apply the idempotent SQL migrations so column-adds / backfills land
    # without a hand-run psql (the manual-DDL trap that 500'd offering reads on
    # 011/012 rolls). See scripts/apply_migrations.py.
    from scripts.apply_migrations import run_from_env as _apply_migrations

    if _apply_migrations() != 0:
        raise MigrationError("SQL migration apply failed — refusing to serve (see logs)")
    # … and finally the platform tenants whose ids the fleet hardcodes. This is
    # NOT redundant with migrations 013/014: apply_all() BASELINES on a schema
    # that already has the app tables — stamping every file applied without
    # running it — and its probe is `to_regclass('public.offering')`, which
    # `create_tables()` above has just satisfied. So on a fresh database those two
    # files never execute and every hardcoded identity is missing. Measured
    # 2026-08-12: prod has all 7, a create_all'd database has none.
    #
    # Placed INSIDE _run_db_bootstrap deliberately, per this function's own
    # contract: the deferred-retry path re-runs it byte-identically, so a pod that
    # boots against a briefly-unreachable database still seeds before it reports
    # ready. Idempotent (ON CONFLICT DO NOTHING), so every boot is a no-op once
    # the rows exist. A failure here is not a connect fault, so it propagates and
    # the pod refuses to serve — the same fail-fast this function already applies
    # to a broken migration.
    db_manager.seed_platform_tenants()
    # … and finally REPORT any tenant id that is also a Keycloak PRINCIPAL id (a
    # `subject.keycloak_sub` or a `subject.id`). Placed LAST on purpose: it must
    # read the schema after migrations AND the rows after the seed, since the
    # seed itself re-inserts two of the three ids that violate the invariant on
    # prod today.
    #
    # ⛔ REPORT, NEVER RAISE, and `report_at_boot` swallows its own exceptions to
    # guarantee it. Everything above this line is allowed to refuse to serve;
    # this is not. Three rows violate today, and a check that CrashLooped both
    # replicas of the platform auth path over a weeks-old billing-hygiene defect
    # would be a worse outage than the defect. Enforcement of NEW rows lives in
    # `DatabaseManager.ensure_tenant`, which refuses them at the primitive; the
    # standing measurement is the `/metrics` gauge
    # `nonagentic_license_tenant_is_subject_rows{class,match,source}`.
    #
    # ⚠️ It also WARNs, on every boot, about the door it cannot open: a tenant
    # whose Keycloak principal was never written to `subject` (`nonagentic-gtm`)
    # is invisible to it, and a clean reading is exactly when that matters most.
    report_tenant_subject_violations(db_manager.get_session)


async def _retry_db_bootstrap_until_ready(app: FastAPI) -> None:
    """Re-run the bootstrap until the database comes back, then report ready.

    Runs the blocking work through ``asyncio.to_thread`` so a retry against a
    still-dead database cannot park the event loop — the exact fault that made
    ``/health/live`` miss its own probe timeout and got gitops #2553 reverted.
    """
    while True:
        await asyncio.sleep(DB_BOOTSTRAP_RETRY_SECONDS)
        try:
            await asyncio.to_thread(_run_db_bootstrap)
        except Exception as exc:
            if is_db_unreachable(exc):
                logger.warning(
                    "db_bootstrap_retry_pending: database still unreachable, "
                    "serving 503 on /health/ready (retry in %ss)",
                    DB_BOOTSTRAP_RETRY_SECONDS,
                )
                continue
            # The database came back and the schema work FAILED on its merits.
            # Stop retrying and stay permanently unready rather than exiting:
            # with ``maxUnavailable: 0`` the previous pods keep serving and the
            # rollout stalls visibly, which beats CrashLoopBackOff on both
            # replicas of the platform's auth service.
            logger.critical(
                "db_bootstrap_failed_permanently: the database is reachable but the "
                "schema could not be brought up to date. This pod will stay NOT READY "
                "and will not retry. Fix the migration and roll again.",
                exc_info=True,
            )
            return
        app.state.db_bootstrap_ok = True
        logger.info("db_bootstrap_recovered: database reachable, reporting ready")
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events (startup/shutdown)."""
    # Startup
    logger.info(f"Starting {SERVICE_NAME} v{VERSION} ({ENVIRONMENT})")

    # WHY THIS IS NOT A BARE ``db_manager.create_tables()`` ANY MORE.
    #
    # It used to be, and an unreachable database at boot therefore raised
    # straight out of lifespan: uvicorn exits, kubelet restarts, backoff grows.
    # Because THIS service backs ext_authz for the whole platform at
    # ``replicas: 2``, a database blip that killed one container took the pod
    # into CrashLoopBackOff and kept it there — MEASURED 2026-08-11 in a scratch
    # namespace: postgres had been back for 40 SECONDS and the pod was still
    # CrashLoopBackOff at 5 restarts. Fail-fast delayed the recovery it was
    # supposed to protect.
    #
    # Fail-fast is right for a BROKEN MIGRATION and wrong for a TRANSIENT
    # OUTAGE, and the two are already structurally distinguishable — no
    # heuristics, no string matching:
    #   * unreachable database -> the connect raises (SQLAlchemy inside
    #     create_tables(), raw psycopg inside apply_all's own connect);
    #   * a migration that ran and failed -> apply_all CATCHES it and returns 1,
    #     which _run_db_bootstrap turns into MigrationError.
    # ``is_db_unreachable()`` (models.py) is the single predicate for that fork.
    #
    # The flag is ASSERTED here, not inherited. It is the pod's readiness state
    # machine, and the deferred branch below only ever leaves it alone — so a
    # lifespan entered after any earlier successful boot in the same process
    # would start from True and report READY while its own bootstrap was
    # failing. Found by the full-suite run of
    # tests/unit/test_boot_resilience.py, which passed in isolation and failed
    # once a prior test had booted the app successfully.
    app.state.db_bootstrap_ok = False
    bootstrap_deferred = False
    try:
        _run_db_bootstrap()
    except Exception as exc:
        # MigrationError is not a connect fault, so this re-raises it and the
        # pre-existing "refusing to serve" behaviour is preserved EXACTLY where
        # it belongs. Only an unreachable database falls through.
        if not is_db_unreachable(exc):
            raise
        bootstrap_deferred = True
        logger.error(
            "db_bootstrap_deferred: database unreachable at startup. Serving "
            "LIVE but NOT READY (/health/ready -> 503) and retrying every %ss "
            "instead of exiting — exiting here would CrashLoopBackOff both "
            "replicas of the platform auth path.",
            DB_BOOTSTRAP_RETRY_SECONDS,
            exc_info=True,
        )
    else:
        app.state.db_bootstrap_ok = True
        logger.info("Database initialized: %s", redact_dsn(DATABASE_URL))

    # Bound BOTH database-facing thread pools — see HANDLER_THREAD_LIMIT for the
    # pgbouncer arithmetic. This MUST happen here rather than at import time:
    # anyio's default thread limiter is a RunVar keyed on the RUNNING event loop
    # (anyio/lowlevel.py: RunVar._current_vars -> current_token().native_token),
    # so a value set outside the loop lands in a different store and never
    # reaches the request path.
    anyio.to_thread.current_default_thread_limiter().total_tokens = HANDLER_THREAD_LIMIT

    # ``asyncio.to_thread`` dispatches to ``loop.run_in_executor(None, …)``, which
    # anyio's limiter does not see. Replacing the loop's default executor is safe
    # for uvicorn: its only ``run_in_executor`` call site is
    # ``uvicorn/middleware/wsgi.py``, which passes its OWN explicitly-constructed
    # ThreadPoolExecutor and is not on this (pure ASGI) app's request path.
    to_thread_executor = ThreadPoolExecutor(
        max_workers=TO_THREAD_EXECUTOR_LIMIT, thread_name_prefix="licenses-to-thread"
    )
    asyncio.get_running_loop().set_default_executor(to_thread_executor)
    logger.info(
        "DB threadpools bounded: %d anyio handler threads + %d to_thread executor threads "
        "(%d client connections per pod, against a 40-connection pgbouncer budget)",
        HANDLER_THREAD_LIMIT,
        TO_THREAD_EXECUTOR_LIMIT,
        HANDLER_THREAD_LIMIT + TO_THREAD_EXECUTOR_LIMIT,
    )

    # Spawned AFTER the governors above, deliberately: the retry does its
    # blocking work through ``asyncio.to_thread``, which dispatches to the loop's
    # default executor. Started any earlier it would run on the stock executor
    # (min(32, cpu+4) threads) and escape the connection budget the governors
    # exist to enforce.
    bootstrap_task = (
        asyncio.create_task(_retry_db_bootstrap_until_ready(app)) if bootstrap_deferred else None
    )

    logger.info(f"{SERVICE_NAME} ready!")

    try:
        yield
    finally:
        if bootstrap_task is not None:
            bootstrap_task.cancel()
            # Await the cancellation so the task cannot still be inside
            # ``to_thread`` when the executor below is shut down.
            with suppress(asyncio.CancelledError):
                await bootstrap_task
        # Shutdown. uvicorn drains in-flight requests BEFORE running lifespan
        # shutdown, so wait=True here should have nothing left to wait for — and
        # if it does, that work is a database write we would rather finish than
        # cancel.
        logger.info(f"Shutting down {SERVICE_NAME}")
        to_thread_executor.shutdown(wait=True)


app = FastAPI(
    title=SERVICE_NAME,
    description="Internal licensing service for the nonagentic.ai platform. Manages application entitlements, plans, and API tokens. Provides /license/introspect for Istio ext_authz token validation and tenant context injection across the service mesh.",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    # /openapi.json is always served — clients (orval, OpenAPI tooling) need the spec
    # in production. Only the Swagger/ReDoc UIs are gated by DOCS_ENABLED.
    openapi_url="/openapi.json",
)

# Has the schema been brought up to date? ``/health/ready`` reports 503 until it
# has. Initialised HERE, at module scope, rather than only inside ``lifespan``:
# read from a handler, a missing attribute would be an AttributeError -> 500 on
# the readiness probe, so the safe value has to exist before any request can
# arrive. False is the safe default in both directions — a pod that never ran
# lifespan has not migrated anything and must not be sent traffic.
app.state.db_bootstrap_ok = False

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add authentication middleware (conditional on ENABLE_AUTH)
if ENABLE_AUTH:
    app.add_middleware(AuthMiddleware)
    logger.info("Authentication middleware enabled")
else:
    logger.warning("Authentication middleware DISABLED (ENABLE_AUTH=false)")

# Access log (logger ``licenses.access``): added AFTER AuthMiddleware so it is the
# OUTER layer — it sees the final status (incl. AuthMiddleware's own 401/403) and
# reads the user AuthMiddleware stored on the request state.
app.add_middleware(AccessLogMiddleware)


# Custom OpenAPI: inject bearer auth + the tenant header (no concrete tenant id)
def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    components = openapi_schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.setdefault(
        "BearerAuth",
        {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Paste the raw license token. Swagger UI sends it as Authorization: Bearer <token>."
            ),
        },
    )

    parameters = components.setdefault("parameters", {})
    parameters.setdefault(
        "TenantIdHeader",
        {
            "name": "X-Tenant-Id",
            "in": "header",
            "required": False,
            # NO `default` and NO `example`, deliberately. The fallback is a
            # SERVER-side value, and a generated client materialises a documented
            # default into real requests. In this fleet a bare X-Tenant-Id is the
            # gateway's production contract, so publishing any concrete tenant id
            # here turns "I used the documented default" into a cross-tenant
            # request. Callers send their OWN tenant id or omit the header.
            "description": (
                "Tenant identifier. Omit to accept the service-side default; "
                "when sent, it must be the caller's own tenant id."
            ),
            "schema": {"type": "string"},
        },
    )

    for path, path_item in openapi_schema.get("paths", {}).items():
        normalized_path = path.rstrip("/") or "/"
        requires_auth = any(normalized_path.startswith(prefix) for prefix in PROTECTED_PREFIXES)

        for method, operation in list(path_item.items()):
            if not isinstance(operation, dict):
                continue
            op_params = operation.setdefault("parameters", [])
            has_tenant = any(
                p.get("name", "").lower() == "x-tenant-id" and p.get("in") == "header"
                for p in op_params
            )
            if not has_tenant:
                op_params.append({"$ref": "#/components/parameters/TenantIdHeader"})

            # AuthMiddleware.dispatch matches _BYO_MODE_OPEN_ROUTES and returns BEFORE it
            # consults PROTECTED_PREFIXES, so deriving `security` from the prefix set alone
            # publishes auth this GET does not require — and a generated client that
            # believes it will keep sending a token it never needed, while a reader
            # "cleaning up" the exemption has a document agreeing with them.
            #
            # _BYO_MODE_OPEN_ROUTES is written for CONCRETE request paths; it matches the
            # TEMPLATE too because a FastAPI {param} is one slash-free segment, which is
            # exactly what [^/]+ accepts. That is a coincidence, so it is pinned by
            # tests/unit/test_endpoints.py::TestOpenAPISchema as an EXACT set — if it ever
            # stops holding, the set empties and the test says so.
            #
            # METHOD-AWARE on purpose: only GET is exempt. PATCH here, and PUT/DELETE on
            # the per-app shape, keep their license-admin gate and must keep their stamp.
            tokenless_s2s = method == "get" and bool(
                _BYO_MODE_OPEN_ROUTES.fullmatch(normalized_path)
            )

            if requires_auth and not tokenless_s2s and "security" not in operation:
                operation["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


object.__setattr__(app, "openapi", custom_openapi)


# ============= Exception Handlers =============

# Shortest cause text we will treat as "contained in the detail". A 1-2 character
# exception message can appear inside a legitimate static detail by pure accident;
# below this length the containment test is not evidence of a leak.
_MIN_CAUSE_LEN_FOR_LEAK = 4
_SCRUBBED_DETAIL = "Internal server error"


def _detail_strings(detail: object, depth: int = 0) -> list[str]:
    """Every string reachable inside ``detail``, however it is nested.

    ``detail`` is not always a string. ``GET /licenses/active`` returns
    ``detail={"error": "no_active_license", ...}``, so a future 5xx built the same way
    — ``detail={"error": str(e)}`` — would have sailed past a ``isinstance(detail, str)``
    guard untouched. Dict keys count too: a leak can just as easily be keyed by the
    exception text as valued by it.
    """
    if depth > 6:
        return []
    if isinstance(detail, str):
        return [detail]
    if isinstance(detail, dict):
        out: list[str] = []
        for key, value in detail.items():
            out += _detail_strings(key, depth + 1)
            out += _detail_strings(value, depth + 1)
        return out
    if isinstance(detail, list | tuple | set | frozenset):
        out = []
        for item in detail:
            out += _detail_strings(item, depth + 1)
        return out
    return []


def _leaked_cause(exc: HTTPException) -> str | None:
    """Return the cause text that ``exc.detail`` is leaking, or None.

    This is a containment test, not a heuristic: an ``HTTPException`` raised from
    inside an ``except`` block carries the caught exception on ``__cause__``
    (explicit ``raise ... from e``) or ``__context__`` (implicit). If the *text of
    that exception* literally appears inside ``detail``, then ``detail`` was built
    by interpolating it — which is exactly the defect. A static detail chosen by
    the developer ("Database not ready") does not contain its own cause, so it
    passes through untouched.

    NOTE this cannot see a leak whose handler has already EXITED: once the ``except``
    block is left, both ``__cause__`` and ``__context__`` are ``None``, so there is no
    cause to compare against. That shape is caught statically instead — see
    ``tests/unit/test_no_exception_leak.py`` on function-scoped taint.
    """
    haystacks = [s for s in _detail_strings(exc.detail) if s]
    if not haystacks:
        return None
    seen: set[int] = set()
    cause: BaseException | None = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        text = str(cause).strip()
        # NB `"" in detail` is always True — the length floor is what makes this
        # test meaningful, not decoration.
        if len(text) >= _MIN_CAUSE_LEN_FOR_LEAK and any(text in h for h in haystacks):
            return text
        cause = cause.__cause__ or cause.__context__
    return None


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Second line of defence against leaking a caught exception to the caller.

    The source-level invariant (``tests/unit/test_no_exception_leak.py``) is the
    primary control; this catches a 5xx site that somehow slips past it.

    SCOPED TO 5xx ON PURPOSE. 4xx details are load-bearing contract:
    ``tests/auth/test_rbac.py`` asserts role names in 403 bodies, and the 409 from
    ``POST /tenants/{tenant_id}`` carries the name-conflict message that is the
    caller's only actionable signal. Sanitising 4xx would break both.
    """
    if exc.status_code >= 500:
        leaked = _leaked_cause(exc)
        if leaked is not None:
            logger.error(
                "HTTPException detail leaked its cause (scrubbed before send): %s",
                exc.detail,
                exc_info=True,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": _SCRUBBED_DETAIL},
                headers=exc.headers,
            )
    return await fastapi_http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> Response:
    """Last-resort handler for anything that escapes the user middleware stack.

    ⚠️ THIS HANDLER RUNS OUTSIDE ``ExceptionMiddleware``, which is the whole
    reason the ``HTTPException`` branch below exists and the reason it used to be
    broken. ``build_middleware_stack`` puts the handler registered for
    ``Exception`` on ``ServerErrorMiddleware`` — the OUTERMOST layer — while the
    handler registered for ``HTTPException`` lives on ``ExceptionMiddleware``, the
    innermost one, wrapping only the router:

        ServerErrorMiddleware(handler=global_exception_handler)   <- outermost
          AuthMiddleware (BaseHTTPMiddleware)
            CORSMiddleware
              ExceptionMiddleware(handlers={HTTPException: http_exception_handler})
                router

    So an ``HTTPException`` raised in a ROUTE or a dependency never arrives here —
    it is caught one layer in. The only way one reaches this function is by being
    raised in the user middleware layer, i.e. above ``ExceptionMiddleware``:
    today that means ``AuthMiddleware``, whose ``validate_tenant_uuid`` calls
    raise ``HTTPException`` for a malformed tenant.

    This branch used to be ``raise exc``, on the belief that re-raising would let
    "FastAPI's HTTPException handler return its intended response". It cannot.
    ``ServerErrorMiddleware`` awaits this handler and only then sends its result;
    an exception raised HERE means control never reaches that send, so nothing is
    written to the wire. Measured against the installed starlette: a
    middleware-raised ``HTTPException(400)`` produced **500 with an empty body and
    no headers**. Status, JSON body and ``WWW-Authenticate`` were all lost.

    Delegating to this module's own ``http_exception_handler`` — rather than
    building a response inline — also means the 5xx detail scrubbing it performs
    applies on this path too, instead of being bypassed by it.
    """
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)

    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Internal server error",
                "status": 500,
            }
        },
    )


# ============= Pydantic Models =============


class HealthResponse(BaseModel):
    """Health check response"""

    status: str
    service: str
    version: str
    environment: str
    timestamp: str
    database: str


class LivenessResponse(BaseModel):
    """Kubernetes liveness probe response"""

    status: str


class ReadinessResponse(BaseModel):
    """Kubernetes readiness probe response"""

    status: str
    database: str


# ============= Error envelope =============
# A single shared error shape so orval generates a named TS error type rather
# than re-deriving an anonymous one per route. FastAPI's default for HTTPException
# is `{"detail": ...}` so we mirror that — `detail` may be a string or a
# structured dict (e.g. /licenses/active uses {error, tenant_id, app_key}).
class ErrorResponse(BaseModel):
    """Standard error envelope returned by HTTPException-derived errors."""

    detail: Any = Field(
        ...,
        description=(
            "Error description — string for simple errors, object for structured "
            "details (e.g. {'error': 'no_active_license', 'tenant_id': '...'})."
        ),
    )


COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Bad request (invalid UUID, malformed body)"},
    401: {"model": ErrorResponse, "description": "Unauthorized (missing or invalid token)"},
    403: {"model": ErrorResponse, "description": "Forbidden (insufficient role)"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}

#: The 403 of every per-app catalogue write gated by ``_authorize_app_admin``
#: (plans, offerings, app delete). Merged over COMMON_ERROR_RESPONSES' generic 403.
APP_ADMIN_403_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: {
        "model": ErrorResponse,
        "description": (
            "Forbidden; nothing was written. The caller lacks the required role, or "
            "`detail.error` is `unknown_api_client` (the client is not a registered "
            "owning app) or `app_not_administrable_by_caller` (the caller's owner "
            "does not issue this app — `app_issuer`)."
        ),
    },
}

#: The 403 of the per-APP tenant settings (``_resolve_tenant_for_app_setting``):
#: per-app BYO mode and offering provision / unprovision.
APP_SETTING_403_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: {
        "model": ErrorResponse,
        "description": (
            "Forbidden; nothing was written. The caller lacks the required role, or "
            "`detail.error` is `unknown_api_client`; "
            "`app_not_administrable_by_caller` (the tenant's OWNER neither issues "
            "this app nor brought it onto the tenant with one of its bundles); "
            "`app_not_issuable_by_caller` (a grantee that does not issue this app); "
            "or `tenant_not_accessible` (neither owner nor grantee)."
        ),
    },
}


# Token models
class TokenIntrospectRequest(BaseModel):
    """Request to introspect a token"""

    token: str = Field(..., description="Raw token to introspect")


class TokenIntrospectResponse(BaseModel):
    """Response from token introspection"""

    active: bool
    tenant_id: str | None = None  # ext_authz injects this as X-Tenant-Id downstream
    license_id: str | None = None
    subject_id: str | None = None
    # The licence subject's Keycloak ``sub`` (``subject.keycloak_sub``). ``subject_id``
    # above is THIS service's own row id, which no other service ever sees on a
    # Keycloak-authenticated path — so a consumer that records "who owns this" from a
    # forwarded Keycloak sub (ingestion's run owner, via kag) could never match it
    # against a licence caller. ADDITIVE: None when the licence has no subject row, and
    # absent on /licenses/active. It identifies; it grants nothing.
    subject_keycloak_sub: str | None = Field(
        None,
        description=(
            "Keycloak `sub` of the licence's subject (the identity other services record "
            "for a Keycloak-authenticated caller). Null unless the stored value is "
            "UUID-shaped and names no subject row. Identification only; grants nothing."
        ),
    )
    # Who ACTS when this licence token is presented (license-authz's metering actor_id).
    # ``subject_keycloak_sub`` when that is set, else the subject's stated service-account
    # sub (``subject.actor_sub``, migration 047). ADDITIVE; absent on /licenses/active.
    subject_actor_sub: str | None = Field(
        None,
        description=(
            "Keycloak `sub` that acts for the licence's subject: `subject_keycloak_sub` "
            "when set, else the subject's stated service-account sub. Null when neither "
            "is known. Identification only; grants nothing."
        ),
    )
    # Tenant-level holder vs a person (models.is_tenant_level_subject on the raw
    # keycloak_sub + realm). Lets a consumer grant tenant-scoped capability (MCP's
    # care-bus tool scope) to the tenant's bundle licence ``svc:<tenant>:<app>`` only,
    # never to a member's per-login licence — which ``subject_keycloak_sub`` cannot
    # tell apart (null for both when the member's stored sub does not qualify).
    # ADDITIVE; absent on /licenses/active.
    subject_kind: Literal["tenant", "member"] | None = Field(
        None,
        description=(
            "`tenant` for a tenant-level holder (a `svc:<tenant>:<app>` service subject "
            "or a non-human realm), `member` for a person's licence. Null when the "
            "licence has no subject row, and on GET /licenses/active."
        ),
    )
    byo_mode: str | None = None  # 'local_allowed' | 'byo_required' | 'platform_only'
    app: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    status: str | None = None
    starts_at: str | None = None
    expires_at: str | None = None
    # #240: the presented token's own expiry, so a caller can refresh before it lapses.
    # ADDITIVE; None on /licenses/active (no token there) and on an inactive answer.
    token_expires_at: str | None = Field(
        None,
        description=(
            "This token's own expiry (ISO 8601); null = the token never expires, OR (on "
            "GET /licenses/active, which has no token) not applicable. Independent of the "
            "licence-level expires_at."
        ),
    )
    region_id: str | None = None
    metadata: dict[str, Any] | None = None
    reason: str | None = None
    # Roles THIS TOKEN carries (migration 038). `[]` for every ordinary token; non-empty
    # only for a short-lived operator token minted by POST /licenses/{id}/operator-token.
    # ADDITIVE: None (absent) on /licenses/active and on an inactive answer, so no
    # existing consumer sees a changed payload except a new `roles: []` on active tokens.
    roles: list[str] | None = Field(
        None,
        description=(
            "Realm roles carried by this token. Empty for every ordinary licence token; "
            "`platform-admin` only on a short-lived operator token (see "
            "POST /licenses/{license_id}/operator-token)."
        ),
    )
    # --- /licenses/active pick diagnostics -------------------------------------
    # Populated ONLY by GET /licenses/active, which resolves a (tenant, app)
    # candidate SET down to one license. All three default to None so
    # /license/introspect — which shares this response model — is unaffected and
    # its payload is unchanged.
    candidate_count: int | None = Field(
        None,
        description=(
            "Number of active licenses the (tenant_id, app_key) pair matched. "
            ">1 is NORMAL: seats are minted per subject, apps are sold per tenant."
        ),
    )
    ambiguous: bool | None = Field(
        None,
        description=(
            "True when the returned license was chosen by the deterministic "
            "tenant-level fallback out of MORE THAN ONE candidate — i.e. the "
            "service had to guess which seat the caller meant."
        ),
    )
    subject_match: bool | None = Field(
        None,
        description=(
            "Only set when a subject_id was supplied. True when that subject's "
            "OWN active license was returned; False when the reference could not "
            "be resolved within the tenant's candidates and the fallback was used."
        ),
    )


class TokenRevokeRequest(BaseModel):
    """Request to revoke a token"""

    token: str | None = Field(None, description="Raw token to revoke")
    license_id: str | None = Field(None, description="License ID to revoke all tokens")


class TokenRevokeResponse(BaseModel):
    """Response from token revocation"""

    revoked: bool
    message: str
    #: Dependency licences (``is_dependency``) released in the SAME transaction
    #: because no remaining active licence on the tenant requires them any more.
    #: Empty for a token revoke and when nothing was orphaned.
    released_dependency_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of the tenant's dependency licences revoked with this licence because "
            "no remaining active licence requires them (app_requires closure)."
        ),
    )


# Region models
class RegionCreate(BaseModel):
    """Create a new region"""

    code: str = Field(..., description="Region code (e.g., 'us', 'eu')")
    name: str = Field(..., description="Region name")
    active: bool = Field(True, description="Whether region is active")


class RegionResponse(BaseModel):
    """Region response"""

    id: str
    code: str
    name: str
    active: bool
    created_at: str


# App models
#: Who holds a licence for an app. See ``models.App.audience``.
#: ``platform`` is the default on create: a newly registered app is not offered to
#: people until someone says it should be.
APP_AUDIENCES = ("human", "platform", "automation")


class AppCreate(BaseModel):
    """Create a new app"""

    key: str = Field(..., description="Unique app key")
    name: str = Field(..., description="App name")
    description: str | None = Field(None, description="App description")
    audience: Literal["human", "platform", "automation"] = Field(
        "platform",
        description=(
            "Who holds a licence: 'human' (end-user product), 'platform' "
            "(service-to-service), 'automation' (n8n action module). Defaults to "
            "'platform' — a new app is not offered to people until declared."
        ),
    )
    issuers: list[str] | None = Field(
        None,
        min_length=1,
        description=(
            "Owning apps (tenant_owner keys) that may issue this app's licences "
            "(migration 050 app_issuer). Omitted = the CALLER's own owning app (from "
            "the verified token, api_client). Only the caller's OWN owner may be named "
            "(else 403 `issuer_not_caller_owner`); a cross_owner client may name any "
            "owner, audited. An app is never created without an "
            "issuer. Applied only when the app is created; an existing key is "
            "returned unchanged."
        ),
    )


class AppResponse(BaseModel):
    """App response"""

    id: str
    key: str
    name: str
    description: str | None
    revoked: bool
    # The enum, not a bare str: the column is CHECK-constrained to these three
    # values, and consumers generate clients from docs/openapi.json. fr switches on
    # this field to decide which apps a person may be granted, so a loose `string`
    # would hand it an untyped value to re-validate by hand — which is how the
    # classification ended up hardcoded in fr in the first place.
    audience: Literal["human", "platform", "automation"]
    created_at: str


# Plan models
class PlanCreate(BaseModel):
    """Create a new plan"""

    app_id: UUID = Field(..., description="App ID")
    code: str = Field(..., description="Plan code (e.g., 'test', 'trial', 'full')")
    name: str = Field(..., description="Plan name")
    description: str | None = Field(None, description="Plan description")
    region_id: UUID | None = Field(None, description="Region ID (null for global)")
    duration_days: int | None = Field(None, description="Duration in days (null for perpetual)")
    rate_limit_rpm: int | None = Field(
        None, description="Requests per minute (null for default 60)"
    )
    rate_limit_burst: int | None = Field(None, description="Burst allowance (null for default 10)")
    # ⚠️ DEFAULTS ARE METERED, not unlimited. Omitting these used to mean NULL =
    # UNLIMITED, and that default is where the estate's 42 uncapped plan rows
    # came from (migration 020): a licence on such a plan gets no billing budget
    # at all, so every debit is skipped at INFO with no ceiling and no alert.
    # George, 2026-08-11: "every plan needs to be metered! that's the whole point
    # of having metering!" An operator who genuinely wants an uncapped plan must
    # now say so EXPLICITLY by sending `null`; it is no longer what you get by
    # forgetting the field. See scripts/seed_apps.py for the same two constants
    # and the measurements behind them.
    quota_monthly: int | None = Field(
        1_000_000,
        ge=0,
        description=(
            "Monthly request quota; 0 = block all, null = unlimited. "
            "Defaults to 1,000,000 — omitting it yields a METERED plan, not an unlimited one."
        ),
    )
    token_budget_monthly: int | None = Field(
        50_000_000,
        ge=0,
        # Same floor as quota_monthly above, and for the same reason:
        # ``_provision_budget`` pushes a budget only when the value ``is not
        # None and >= 0``, so a negative stored here is skipped and the licence
        # is then treated as UNLIMITED — the exact silently-unlimited failure
        # the zero-budget fix exists to prevent, re-entered through the front
        # door. 0 = block all, null = unlimited; negative is meaningless.
        description=(
            "Monthly LLM token budget; 0 = block all, null = unlimited. "
            "Defaults to 50,000,000 — ~4.4x the busiest licence's observed monthly peak."
        ),
    )


class PlanUpdate(BaseModel):
    """Update an existing plan (all fields optional)"""

    name: str | None = Field(None, description="Plan name")
    description: str | None = Field(None, description="Plan description")
    duration_days: int | None = Field(None, description="Duration in days")
    rate_limit_rpm: int | None = Field(None, description="Requests per minute")
    rate_limit_burst: int | None = Field(None, description="Burst allowance")
    quota_monthly: int | None = Field(
        None, ge=0, description="Monthly request quota; 0 = block all, null = unlimited"
    )
    token_budget_monthly: int | None = Field(
        None, ge=0, description="Monthly LLM token budget; 0 = block all, null = unlimited"
    )
    # Retiring a plan. A plan referenced by a licence cannot be DELETEd (409),
    # so this is the only way to take one out of the catalogue. Revoked plans
    # are hidden from GET /plans and GET /apps/{app_id}/plans (unless
    # ?include_revoked=true on the latter) and are not chosen for new bundle
    # provisioning; licences ALREADY on the plan keep validating — nothing in
    # mint / introspect reads Plan.revoked. ``false`` un-revokes. ``null`` is
    # rejected: the column is NOT NULL.
    revoked: bool | None = Field(
        None,
        description=(
            "Retire (true) or un-retire (false) the plan. Revoked plans are hidden from "
            "plan listings; existing licences on the plan keep working."
        ),
    )


class PlanResponse(BaseModel):
    """Plan response"""

    id: str
    app_id: str
    code: str
    name: str
    description: str | None
    region_id: str | None
    duration_days: int | None
    rate_limit_rpm: int | None = None
    rate_limit_burst: int | None = None
    quota_monthly: int | None = None
    token_budget_monthly: int | None = None
    revoked: bool
    created_at: str


# License models
class LicenseCreate(BaseModel):
    """Create a new license"""

    app_id: UUID = Field(..., description="App ID")
    plan_id: UUID = Field(..., description="Plan ID")
    region_id: UUID | None = Field(None, description="Region ID")
    # ⚠️ TIMEZONE-AWARE, and the awareness is the point — these were `str`.
    #
    # They were parsed with `datetime.fromisoformat()` in the handler and written
    # straight into `timestamptz` columns. `fromisoformat("2027-01-01T00:00:00")`
    # — a perfectly valid ISO 8601 string, and exactly what the old description
    # "(ISO 8601)" invited — returns a NAIVE datetime. psycopg then hands
    # PostgreSQL a bare timestamp, which it interprets in the SESSION TimeZone.
    # So the same request stored a different instant depending on a server
    # setting, silently, and `License.effective_status` (which compares against
    # `datetime.now(UTC)`) served the wrong entitlement by the UTC offset — to
    # provisioning and to fr's licence gate.
    #
    # Rejecting a naive value is deliberate rather than assuming UTC: assuming is
    # the same guess PostgreSQL was already making, just relocated somewhere the
    # caller cannot see. A 422 names the field and the caller fixes it once.
    #
    # NOT a breaking change: pydantic accepts any RFC 3339 / ISO 8601 STRING for a
    # `datetime` field, so a caller sending "2027-01-01T00:00:00Z" is unaffected.
    # The only producer in the tree, provisioning's
    # `src/clients/licenses_client.py`, passes offset-bearing strings through.
    starts_at: AwareDatetime | None = Field(
        None,
        description=(
            "Start datetime. MUST carry a UTC offset (e.g. 2027-01-01T00:00:00Z); "
            "an offset-less value is rejected with 422 rather than silently "
            "reinterpreted in the database's session timezone."
        ),
    )
    expires_at: AwareDatetime | None = Field(
        None,
        description=(
            "Expiration datetime. MUST carry a UTC offset; an offset-less value "
            "is rejected with 422."
        ),
    )
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")
    # ⛔ THE SUBJECT'S REALM, STATED BY THE MINTING SERVICE — NOT INFERRED FROM THE CALLER.
    #
    # When absent the handler keeps its historical behaviour and stamps a NEW Subject
    # with the realm of the CALLER's token (`user.iss`). That is the realm of whoever
    # ASKED, not of the person the licence is for: CommonPractice's service account
    # authenticates in `secure-apps`, so every employee it licensed became a
    # `secure-apps` Subject (measured 2026-09-17: CP subjects 3 × secure-apps minted by
    # its SA, 2 × portal minted via portal-bff where the caller WAS the human).
    #
    # It is harmless only by luck: seat counting partitions on realm
    # (`human_subjects = subjects − NON_HUMAN_SUBJECT_REALMS − unresolved`) and that
    # tuple is `("service", "cp-tenant")`. Add `secure-apps` to it — plausible, it is the machine
    # realm holding most subjects — and every CP employee silently stops being a seat,
    # and the invoice under-bills to zero without erroring.
    #
    # A minting service knows which realm its users live in, so it says so here.
    # A non-human realm is REFUSED: this field exists to state a PERSON's realm, and
    # accepting `service` would let a caller hide a human from the seat count.
    subject_realm: str | None = Field(
        None,
        description=(
            "Keycloak realm of the HUMAN this licence is for (e.g. 'portal'). Applies "
            "only when the Subject is created by this call; an existing Subject keeps "
            "its realm. Omit to keep the legacy behaviour (the caller's realm). A non-human "
            "realm is refused (422), except 'cp-tenant' for exactly 'cp-tenant:<this licence's tenant id>:a|b' "
            "(CommonPractice's tenant-level holder; not billed as a seat)."
        ),
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
    )

    @field_validator("subject_realm")
    @classmethod
    def _subject_realm_is_human(cls, v: str | None) -> str | None:
        # A stateable non-human realm (`cp-tenant`) is accepted HERE; its sub prefix is
        # enforced in the handler, the only place that sees the path sub.
        if (
            v is not None
            and v in NON_HUMAN_SUBJECT_REALMS
            and v not in STATEABLE_NON_HUMAN_SUBJECT_REALMS
        ):
            raise ValueError(
                f"subject_realm '{v}' is a non-human realm; this field states a person's realm"
            )
        return v


class AppInfo(BaseModel):
    """App information for license response"""

    id: str
    key: str
    name: str


class PlanInfo(BaseModel):
    """Plan information for license response"""

    id: str
    code: str
    name: str
    rate_limit_rpm: int | None = None
    rate_limit_burst: int | None = None
    quota_monthly: int | None = None
    token_budget_monthly: int | None = None


class LicenseResponse(BaseModel):
    """License response"""

    id: str
    subject_id: str
    tenant_id: str
    app_id: str
    plan_id: str
    region_id: str | None
    status: str
    starts_at: str
    expires_at: str | None
    created_at: str
    updated_at: str
    revoked_at: str | None
    # Time-aware validity: active | pending | expired | revoked. `status` above is
    # STRUCTURAL only and overstates validity, because nothing transitions a row
    # out of 'active' when expires_at passes. Consumers were each re-deriving
    # this (provisioning entitlements.py, fr license-status.ts); it is emitted
    # here so there is one rule. Optional with a None default so a response built
    # from a dict that predates the field still validates.
    effective_status: str | None = None
    metadata: dict[str, Any] | None
    # ⚠️ NOT a coverage signal. Migration 018 baselined this across the whole
    # table, so non-NULL can mean "018 stamped this row sight unseen" — measured
    # in prod 2026-08-10, ALL 208 of 208 rows are non-NULL and 205 of them carry
    # that single migration timestamp. Kept for compatibility; read
    # metering_verified_at instead. See migration 019.
    metering_provisioned_at: str | None = None
    # NULL = billing has never been asked what it holds for this licence, so its
    # metering coverage is unknown. Non-NULL = at that moment billing confirmed
    # it held every config the plan owes. Written only by the reconcile sweep,
    # never by a migration and never by a dry run. Additive/optional so existing
    # clients are unaffected.
    metering_verified_at: str | None = None
    # Migration 041 (#239), READ-ONLY here: this licence's own monthly token budget
    # / request quota, pushed to billing (by provisioning AND the reconcile sweep)
    # instead of `plan.token_budget_monthly` / `plan.quota_monthly`. null = the
    # plan's value. Set only by operator provisioning scripts.
    token_budget_override: int | None = None
    quota_override: int | None = None
    # Migration 059: TRUE = a shared DEPENDENCY licence (bundle ensure provisioned it
    # because a bundled app requires this app; released automatically when no active
    # licence on the tenant requires it any more). Migration 039: the bundle a
    # per-app bundle licence was provisioned under (NULL otherwise, always NULL for a
    # dependency licence).
    is_dependency: bool = False
    bundle_id: str | None = None
    app: AppInfo | None = None
    plan: PlanInfo | None = None


def _enrich_license_response(license_obj: Any) -> LicenseResponse:
    """Build a LicenseResponse with nested app and plan data from ORM relationships."""
    data = license_obj.to_dict()
    if license_obj.app:
        data["app"] = AppInfo(
            id=str(license_obj.app.id),
            key=license_obj.app.key,
            name=license_obj.app.name,
        )
    if license_obj.plan:
        data["plan"] = PlanInfo(
            id=str(license_obj.plan.id),
            code=license_obj.plan.code,
            name=license_obj.plan.name,
            rate_limit_rpm=license_obj.plan.rate_limit_rpm,
            rate_limit_burst=license_obj.plan.rate_limit_burst,
            quota_monthly=license_obj.plan.quota_monthly,
            token_budget_monthly=license_obj.plan.token_budget_monthly,
        )
    return LicenseResponse(**data)


class TokenCreateResponse(BaseModel):
    """Response when creating a token"""

    token_id: str
    license_id: str
    token: str  # Raw token (only returned once)
    token_type: str
    created_at: str
    expires_at: str | None
    # Set only when this mint registered a perpetual long-lived token (licenses#244).
    long_lived_owner: str | None = None


class OperatorTokenResponse(TokenCreateResponse):
    """Response when minting a short-lived operator token (raw token returned once)."""

    roles: list[str]


#: The ONLY roles an operator token may carry, and the realm role the caller's own JWT must
#: hold to be granted each. Mirrors the DB CHECK ``ck_token_granted_roles_allowlist``.
OPERATOR_GRANTABLE_ROLES: tuple[str, ...] = ("platform-admin",)
#: Hard cap on an operator token's lifetime. The DB CHECK allows +5 min of clock skew.
OPERATOR_TOKEN_MAX_TTL_MINUTES = 60


class TokenType(StrEnum):
    """Valid token types for license tokens"""

    api = "api"
    node = "node"
    internal = "internal"


# ============= API Endpoints =============


@app.get("/", include_in_schema=False)
def root():
    """Root endpoint - redirects to API documentation"""
    from fastapi.responses import RedirectResponse

    if DOCS_ENABLED:
        return RedirectResponse(url="/docs")
    return {
        "message": "Licensing Service API",
        "docs_enabled": False,
        "hint": "Set ENABLE_DOCS=true or ENVIRONMENT=development to expose Swagger UI.",
    }


# ── Prometheus expiry instrumentation ────────────────────────────────────────
# ⚠️ A `/metrics` ROUTE IS NOT A SCRAPE. Three comments elsewhere in this file
# correctly note that this service has no Prometheus scrape annotation, and the
# counters behind them emit into the void because of it. This route is only half
# the change: the paired gitops PR adds `prometheus.io/scrape` to the licenses
# Deployment pod template, which is what the `kubernetes-pods` job keys on.
# Verified before writing this: `label_values(job)` contains no `licenses`, and
# the pod template carried only a `secret-rev` annotation.
#
# A DEDICATED registry, not the global default: prometheus_client's default
# registry auto-installs process/GC collectors, and this service has never
# exported those. Keeping the surface to exactly the expiry series means the
# scrape cannot start reporting numbers nobody has reasoned about.
_expiry_registry = CollectorRegistry()
_expiry_registry.register(LicenseExpiryCollector(db_manager.get_session))
# The tenant/subject invariant rides the SAME registry and the same /metrics
# route: it is one more SELECT-only snapshot of registry integrity, and giving it
# a second endpoint would need a second scrape annotation to be visible at all.
# ⚠️ Its violation gauge is split by a `class` label so the deliberate
# `default-anon` sentinel cannot pin the actionable series above zero forever,
# by `match` because the two ways an id can be a principal have different fixes,
# and by `source` because this is a SINGLE-SOURCE detector — the companion
# `…_source_measured{source="keycloak_principal"} 0` is what stops a zero being
# read as coverage. See services/tenant_subject_invariant.py.
_expiry_registry.register(TenantSubjectCollector(db_manager.get_session))


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition for licence/token expiry. Public, read-only.

    The collector runs blocking SQL, so it is pushed to a worker thread: an
    async endpoint that blocks the event loop would queue /health/live behind
    it, which is the shape that CrashLoopBackOff'd both replicas in gitops #2553.
    """
    payload = await anyio.to_thread.run_sync(generate_latest, _expiry_registry)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


@app.get("/health", response_model=HealthResponse, operation_id="health_check", tags=["health"])
async def health_check():
    """Process liveness (public - no auth required). Opens no database session.

    Reports `database: "not_checked"`. `/health/ready` is the only endpoint that
    reports database truth.
    """
    # ⚠️ ``database`` MUST NOT become a real check, and this is a security
    # decision rather than an omission. The rationale lives here, in a comment,
    # instead of the docstring on purpose: FastAPI publishes the docstring as
    # the operation description in ``/openapi.json``, which is served
    # unconditionally (see the FastAPI() call below) — an analysis of how to
    # exhaust our connection pool does not belong in a public API spec.
    #
    # What it used to be: the literal ``"connected"``, returned by a handler
    # that never connected. Until gitops #2553 all three k8s probes pointed
    # here, so a pod whose database was gone stayed Ready, stayed in the
    # Service EndpointSlice, and kept serving ext_authz introspects for the
    # whole platform. That manifest change moved readiness to /health/ready
    # (real SELECT 1 -> 503); this change stops the lie at the source. /health
    # is still the STARTUP probe target, a second reason it must stay cheap.
    #
    # Why not "fix" it into a real check:
    #   * this path is INTERNET-REACHABLE AND UNAUTHENTICATED. The Cloudflare
    #     tunnel routes public ``prefix: /licenses/`` to this service
    #     (clusters/strato/istio/tunnel-mtls/virtualservice.yaml) and
    #     ``/licenses/health`` aliases this handler; auth/middleware.py's
    #     PUBLIC_ENDPOINTS exempts both from auth.
    #   * models.py builds the engine with ``poolclass=NullPool``, so every
    #     session is a FRESH connection. A query here turns an unauthenticated
    #     public GET into a connection-exhaustion amplifier aimed at the very
    #     pgbouncer pool it would claim to report on — licences holds 40 server
    #     connections fleet-wide.
    #
    # /health/ready does the real check and does NOT match the public
    # ``/licenses/`` prefix, so it is cluster-only. That asymmetry is the point.
    #
    # Pinned by tests/unit/test_endpoints.py (the value AND a zero-SQL count)
    # and tests/unit/test_probe_contract.py. Must stay ``async``:
    # ``licenses_health_alias`` awaits it.
    return {
        "status": "healthy",
        "service": SERVICE_NAME,
        "version": VERSION,
        "environment": ENVIRONMENT,
        "timestamp": datetime.now(UTC).isoformat(),
        "database": "not_checked",
    }


# Alias so a prefix-rewriting gateway that forwards `/licenses/health` unchanged
# (some deployments route the service under a `/licenses` prefix) gets 200
# instead of 404. Deployment probes and stage-gate checks expect this path.
@app.get(
    "/licenses/health",
    response_model=HealthResponse,
    operation_id="licenses_health_check",
    tags=["health"],
    include_in_schema=False,
)
async def licenses_health_alias():
    return await health_check()


@app.get(
    "/health/live",
    response_model=LivenessResponse,
    operation_id="health_live",
    tags=["health"],
)
async def health_live():
    """Kubernetes liveness probe (public - no auth required). Does no work."""
    # ⚠️ MUST STAY `async`. This is the one handler in this module where async
    # is the SAFE direction, and the reason is a capacity interaction that is
    # deliberately kept out of the docstring — FastAPI publishes docstrings as
    # the operation description in /openapi.json, which is served
    # unconditionally, and our threadpool sizing is not public API.
    #
    # This endpoint touches no database and does no blocking work, so it costs
    # the event loop nothing. Served ON the loop it can always answer.
    #
    # As a sync `def` it is dispatched through the anyio threadpool, bounded to
    # HANDLER_THREAD_LIMIT and SHARED with every DB-backed handler. A liveness
    # probe that competes for the same resource as the workload it monitors
    # cannot do its job: with that pool saturated by requests blocked on the
    # database, /health/live queues behind them and blows its own 5s probe
    # timeout despite having nothing to do. Three misses and kubelet kills the
    # container.
    #
    # MEASURED 2026-08-11, scratch namespace licenses-verify2, database removed
    # with ~10 concurrent DB-backed requests contending for the tokens:
    # readiness correctly returned a real 503 (#115/#116 working) and the pod
    # correctly left the EndpointSlice -- and then LIVENESS still failed and
    # killed the container. The restart cannot recover while the DB is down,
    # because lifespan's create_tables()/apply_migrations() fail-fast: the
    # container exits code 3 in ~10s and enters CrashLoopBackOff. One liveness
    # miss becomes a total outage of BOTH replicas.
    #
    # That run is why gitops #2553 (readiness -> /health/ready, liveness ->
    # /health/live) is still reverted; this is the remaining precondition for
    # re-landing it.
    #
    # Leg (d) of the route-registry invariant
    # (tests/unit/test_event_loop_blocking_invariant.py) encodes the general
    # rule -- a handler that makes no calls must stay on the loop -- so this is
    # a derived requirement, not a hand-maintained exemption.
    return {"status": "alive"}


@app.get(
    "/health/ready",
    response_model=ReadinessResponse,
    operation_id="health_ready",
    tags=["health"],
    responses={503: {"model": ErrorResponse, "description": "Database not ready"}},
)
def health_ready():
    """
    Kubernetes readiness probe (public - no auth required)

    Checks database connectivity before reporting ready.
    """
    # ⚠️ CONNECTIVITY IS NOT SUFFICIENT — the schema has to exist too, and this
    # check is why. When the database is unreachable at boot, ``lifespan`` now
    # serves unready and retries in the background instead of exiting. The
    # database can therefore come back BEFORE ``create_tables()`` /
    # the migrations have run, and in that window ``SELECT 1`` answers perfectly
    # happily. Reporting ready on that would rejoin the Service EndpointSlice
    # and take ext_authz traffic against a schema that is missing tables or
    # columns — trading a clean 503 for a wave of 500s, which is worse.
    #
    # Also covers the permanent case: when a retry finds the database up but the
    # migration genuinely broken, the flag stays False forever and this pod
    # never takes traffic.
    if not app.state.db_bootstrap_ok:
        # No caught exception here, so a static detail is raised directly rather
        # than through ``_internal_error`` — which would log ERROR with a
        # traceback on EVERY probe (once per 10s per pod) for a state that is
        # already reported once by lifespan and once per retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not ready",
        )
    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            session.execute(text("SELECT 1"))
            return {"status": "ready", "database": "connected"}
        finally:
            session.close()
    # Top-level readiness probe: ANY failure (DB driver, connection pool, network) means
    # not-ready — must catch broadly so the probe reports 503 instead of crashing the worker.
    except Exception as e:
        raise _internal_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Database not ready", e, "Readiness check failed"
        ) from e


# ============= Token Endpoints =============


@app.post(
    "/license/introspect",
    response_model=TokenIntrospectResponse,
    operation_id="introspect_token",
    tags=["tokens"],
    responses={
        **COMMON_ERROR_RESPONSES,
        401: {
            "model": ErrorResponse,
            "description": (
                "`introspect_requires_auth` — only reachable when this deployment sets "
                "`INTROSPECT_REQUIRE_AUTH=true`, which is NOT the default. The endpoint is "
                "otherwise public: the token in the body is the credential. Loopback "
                "callers (this service's own middleware, over 127.0.0.1) are exempt even "
                "when armed. 36 of the 38 measured callers in the estate are "
                "unauthenticated today — including the Istio ext_authz enforcer — so do "
                "not assume this status is reachable; see the route docstring."
            ),
        },
    },
)
async def introspect_token(
    request: TokenIntrospectRequest,
    http_request: Request,
):
    """
    Introspect a token to check validity and get license info

    Public endpoint by default — the token in the body is the auth. Gated by
    `INTROSPECT_REQUIRE_AUTH` (default **False**).

    ⛔ WHAT IS WRONG WITH IT BEING PUBLIC. It takes no credential, has no rate
    limit, and answers with `tenant_id`, `license_id`, `subject_id`, plan and
    `byo_mode`. Anyone who can reach the pod CIDR can therefore turn a captured
    token into a tenant identity and an entitlement profile, and can probe tokens
    at line rate. `/licenses/active` — the same response model — was closed to
    `require_internal_or_reader` for exactly this reason (cross-tenant IDOR,
    2026-08-01); this route never was.

    ⛔ AND WHY THE FLAG IS OFF. This is not an oversight to be corrected by
    flipping a boolean; the endpoint is the fleet's authentication primitive.
    MEASURED across the Movies tree: **38 call sites, 36 of them unauthenticated**,
    and the unauthenticated set includes:

      * `license-authz/src/main.py:718` — the Istio **ext_authz** enforcer, one POST
        per mesh request carrying a token. The hottest caller in the estate.
      * **this service's own middleware** — `auth/middleware.py`
        `_introspect_license_token` POSTs to `127.0.0.1:$PORT/license/introspect`
        with no headers, so arming this WITHOUT the loopback exemption below would
        make the service refuse its own opaque-token authentication path.
      * 26 sibling services running a copy of the same per-request middleware, two
        of them SHARED LIBRARIES (`kag_common`, `w2v_common`) through which eight
        further services inherit the call.
      * the TS/gateway clients: `api-gateway`, `commonpractice` (whose own
        docstring documents the endpoint as "public, no auth"), `kanban`,
        `kanban-mcp`, `license-validator`.

    Only `admin_app` (`services/license_client.py:230`, SA bearer — and it
    DEGRADES to unauthenticated when the token mint returns falsy) and the
    `n8n-nodes-licenses` node present a credential today. Two gitops objects also
    assert the endpoint is public and must change with it:
    `policies/envoy-filters/ext-authz-enforce.yaml:439` (ext_authz path carve-out)
    and `services/oauth2-proxy/deployment.yaml:110` (`--skip-auth-route`).

    ⚠️ LOOPBACK IS EXEMPT EVEN WHEN ARMED, and that is a deliberate hole rather
    than an accident: the caller is this same process, reached over `127.0.0.1`,
    which cannot be spoofed from outside the pod's network namespace, and it has
    no credential to present — it is authenticating a token on behalf of a caller
    who has not been authenticated yet. Without this exemption the flag cannot be
    armed at all, in any deployment.

    See `INTROSPECT_REQUIRE_AUTH` for the ordered list of what must change before
    it can go True.
    """
    if INTROSPECT_REQUIRE_AUTH and not _is_loopback_client(http_request):
        # The middleware does not enforce this route (it is absent from
        # PROTECTED_PREFIXES, and `/license/revoke` is listed there rather than
        # `/license`), so BOTH halves have to be here: the check AND the
        # authentication it checks. `_introspect_caller` verifies the bearer with
        # the middleware's own primitive — reading `request.state.user` alone
        # refused every caller, because nothing populates it on an unprotected
        # path. A dependency would be cleaner but would run BEFORE this exemption
        # and so could not make it.
        user = await _introspect_caller(http_request)
        if user is None:
            logger.warning(
                "introspect_unauthenticated_refused: client=%r — INTROSPECT_REQUIRE_AUTH "
                "is armed and this caller presented no verified credential",
                http_request.client.host if http_request.client else None,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "introspect_requires_auth"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    try:
        # ``introspect_token`` is synchronous, blocking DB I/O. Awaiting it via a
        # worker thread keeps this ``async`` handler from serialising the event
        # loop: this is the fleet's hottest auth path (every KAG on-behalf-of +
        # WS-2 re-introspects every ~5 min), and running the blocking call inline
        # made concurrent introspects queue head-of-line — 30 concurrent took
        # ~10s wall and blew past callers' introspection timeouts.
        result = await asyncio.to_thread(db_manager.introspect_token, request.token)

        if result is None:
            return TokenIntrospectResponse(active=False, reason="token_not_found")

        return TokenIntrospectResponse(**result)

    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        # This endpoint is PUBLIC (the token is the auth), so its 500 body is
        # readable by anyone who can reach the service. The global exception
        # handler re-raises HTTPException UNCHANGED, so it cannot sanitize this
        # detail for us — interpolating ``str(e)`` here shipped raw DB/DSN and
        # driver text straight to the caller. Log the real cause, return a
        # generic detail.
        logger.error(f"Token introspection failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


def _select_active_license(
    licenses: list[License], subject_ref: str | None
) -> tuple[License, bool]:
    """Resolve a (tenant, app) candidate SET down to exactly one license.

    Returns ``(license, subject_matched)``.

    ``subject_ref`` is a CALLER-SUPPLIED reference to one seat. It is resolved
    against BOTH identifiers, in this order:

      1. ``Subject.keycloak_sub`` — the Keycloak ``sub`` claim. This is the ONLY
         form license-authz ever holds, so it is the form the real caller sends.
      2. ``Subject.id`` — the locally minted uuid7 primary key.

    Both are UUID-shaped and indistinguishable by inspection, so resolving only
    one form would make every caller silently fall through to the tenant-level
    pick: the bug would look fixed and would not be.

    An unresolvable reference DEGRADES to the fallback, never denies — a caller
    whose tenant holds a license but who holds none of its own must keep working
    exactly as before. Resolution is scoped to ``licenses``, which the caller is
    already authorized to see, so this cannot reach another tenant's rows and
    creates no subject-existence oracle (unknown and known-elsewhere both read
    as "no match").

    The fallback is ``licenses[0]``: the DB already returned the candidates in
    ``ACTIVE_LICENSE_PICK_ORDER`` (models.py), so element 0 is the deterministic,
    replica-identical best seat.
    """
    ref = (subject_ref or "").strip()
    if ref:
        for lic in licenses:
            subject = lic.subject
            if subject is not None and str(subject.keycloak_sub) == ref:
                return lic, True
        try:
            ref_uuid = UUID(ref)
        except ValueError, AttributeError, TypeError:
            ref_uuid = None
        if ref_uuid is not None:
            for lic in licenses:
                if lic.subject_id == ref_uuid:
                    return lic, True
    return licenses[0], False


async def _enforce_own_tenant_scope(
    request: Request,
    *,
    user: User,
    tenant_id: str,
    delegated_roles: set[str],
    detail: str,
) -> None:
    """Restrict a caller that holds no DELEGATION role to its own tenant.

    ``tenant_id`` on the entitlement reads is a caller-controlled QUERY
    parameter, so the route's role gate authorizes NOTHING about *whose*
    entitlement is returned. A caller holding one of ``delegated_roles`` is a
    legitimate cross-tenant S2S lookup surface and passes through; everyone else
    may name ONLY the tenant the middleware already reconciled onto
    ``request.state.user`` (the X-Tenant-Id header is honoured there only for
    admins / trusted proxies), never the raw query value.

    Extracted from ``get_active_license`` and kept extracted deliberately: this
    is the SOLE cross-tenant guard on ``GET /licenses/active``, and a
    hand-written second copy of it is exactly how one of two copies ends up
    subtly weaker. Any future entitlement read must call this, not re-derive it.
    """
    # Migration 072 (realm-per-app P0): a token from an app-owned realm may name
    # ONLY that app's tenants (owned or granted) — checked BEFORE the delegation
    # bypass, because that realm's admin controls its tokens' roles as well.
    if user.realm_owner_key is not None and not await asyncio.to_thread(
        realm_owner_permits, user.realm_owner_key, tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=realm_owner_mismatch_detail()
        )
    if set(user.get_all_roles()) & delegated_roles:
        return
    caller_tenant = await get_tenant_id(request)
    try:
        requested_tenant = str(UUID(str(tenant_id)))
    except ValueError, AttributeError, TypeError:
        requested_tenant = None
    if requested_tenant is None or requested_tenant != caller_tenant:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def _resolve_active_entitlement(
    *,
    tenant_id: str,
    app_key: str,
    subject_id: str | None,
    ctx: str,
) -> TokenIntrospectResponse:
    """Lookup body behind the active-entitlement read.

    AUTHORIZATION IS NOT DONE HERE — the caller must have applied its own gate
    and ``_enforce_own_tenant_scope`` before calling this.

    ``ctx`` names the calling route in the 500 log line only.
    """
    try:
        # Off the event loop — see the comment on ``introspect_token``. Both
        # caller is ``async def`` (it awaits the tenant-scope guard), so
        # FastAPI will NOT move this to the threadpool for us; the blocking DB
        # call has to be offloaded explicitly or it serialises every other
        # request on this worker. ``get_active_license`` eager-loads
        # app/plan/subject, so the attribute reads below issue no further SQL.
        licenses = await asyncio.to_thread(
            db_manager.get_active_license, tenant_id=tenant_id, app_key=app_key
        )

        if not licenses:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "no_active_license",
                    "tenant_id": tenant_id,
                    "app_key": app_key,
                },
            )

        lic, subject_matched = _select_active_license(licenses, subject_id)
        ambiguous = len(licenses) > 1 and not subject_matched

        if ambiguous:
            # There is no prometheus client in this service, so operator
            # visibility is a structured WARN. It fires ONLY here — the fallback
            # path with more than one candidate, i.e. "we had to guess which
            # seat". It deliberately does NOT fire merely because >1 license
            # exists: under the per-org model that is a tenant with two
            # employees, and alerting on it would page on success.
            logger.warning(
                "ambiguous_active_license: picked one of %d active seats for "
                "tenant_id=%s app_key=%s license_id=%s subject_id_supplied=%s "
                "(deterministic ACTIVE_LICENSE_PICK_ORDER; not an error)",
                len(licenses),
                tenant_id,
                app_key,
                str(lic.id),
                bool((subject_id or "").strip()),
            )

        return TokenIntrospectResponse(
            active=True,
            tenant_id=str(lic.tenant_id),
            license_id=str(lic.id),
            subject_id=str(lic.subject_id),
            app=lic.app.to_dict() if lic.app else None,
            plan=lic.plan.to_dict() if lic.plan else None,
            status=str(lic.status),
            starts_at=lic.starts_at.isoformat() if lic.starts_at else None,
            expires_at=lic.expires_at.isoformat() if lic.expires_at else None,
            region_id=str(lic.region_id) if lic.region_id else None,
            metadata=cast(dict[str, Any], lic.license_metadata) if lic.license_metadata else None,
            candidate_count=len(licenses),
            ambiguous=ambiguous,
            # None (not False) when no subject was asked for — "not applicable",
            # so a caller can tell "did not ask" from "asked and did not match".
            subject_match=subject_matched if (subject_id or "").strip() else None,
        )
    except HTTPException:
        raise
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Active license lookup failed",
            e,
            f"{ctx} failed",
        ) from e


@app.get(
    "/licenses/active",
    response_model=TokenIntrospectResponse,
    operation_id="get_active_license",
    tags=["licenses"],
    responses={
        **COMMON_ERROR_RESPONSES,
        # COMMON_ERROR_RESPONSES' generic 404 cannot name a route-specific discriminator,
        # and this route raises one on the wire.
        403: {
            "model": ErrorResponse,
            "description": (
                "Forbidden. The caller lacks the read role or names another tenant, "
                "or `detail.error` is `realm_owner_mismatch` (migration 072): the "
                "caller's token realm, or the `realm` parameter, is owned by an app "
                "(`realm_owner`) that neither owns this tenant nor holds an active "
                "grant on it."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "No effectively-active license for this (tenant, app). `detail.error` is "
                "`no_active_license`, alongside the `tenant_id` and `app_key` that were "
                "resolved. Note this is time-aware: a license whose row still reads "
                "`status='active'` but whose `expires_at` has passed is NOT returned here, "
                "so this 404 does not imply the subject never had a license."
            ),
        },
    },
)
async def get_active_license(
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
    app_key: str = Query(..., description="App key, e.g. 'n8n-gerard', 'kanban'"),
    subject_id: str | None = Query(
        None,
        description=(
            "OPTIONAL seat selector. Accepts EITHER the Keycloak 'sub' claim "
            "(what license-authz holds) OR the local Subject.id UUID. When it "
            "resolves to a subject holding an active license for (tenant_id, "
            "app_key), that exact license is returned; otherwise the "
            "deterministic tenant-level pick is used. Never causes a denial, and "
            "never widens the caller's tenant scope."
        ),
    ),
    realm: str | None = Query(
        None,
        description=(
            "OPTIONAL Keycloak realm of the END USER the S2S caller (license-authz) "
            "is resolving for (migration 072, realm-per-app P0). When that realm has "
            "a `realm_owner` row, the tenant must be owned by that owner or carry an "
            "active `tenant_app_grant` to it, else 403 `realm_owner_mismatch`. A "
            "realm with no row, or no parameter, changes nothing. It can only NARROW "
            "what the caller's own authority admits — never widen it."
        ),
    ),
    user: User = Depends(require_internal_or_reader),
):
    """
    Resolve the active license for (tenant_id, app_key).

    Used by license-authz JWT-mode: a Keycloak JWT has no opaque license token,
    so we look up entitlement by identity+app instead. Response schema matches
    /license/introspect so callers can share a client.

    MULTIPLE ACTIVE LICENSES ARE NORMAL — this route no longer emits a CONFLICT
    response (fixed 2026-08-08). fr/kanban/n8n are sold as ONE package per TENANT
    (whole customer org), but ``License.subject_id`` is NOT NULL and the partial
    unique index is on ``(tenant_id, app_id, subject_id)``, so provisioning mints
    one row per onboarded USER. Two employees in one org = two active rows, and
    under a per-org model the question "does this tenant hold an active license
    for this app" is answered *yes* by more than one row.

    The removed ``multiple_active_licenses`` conflict branch answered it with an
    error instead. Downstream, license-authz ``jwt_mode.py`` calls
    ``raise_for_status()`` and its ``src/main.py`` maps the resulting
    ``httpx.HTTPError`` to a 503 ``licenses_unavailable``; JWT mode is ENABLED in
    prod, so every two-employee tenant got a tenant-wide 503 on
    ``api.nonagentic.ai``, reported as a licensing OUTAGE that was not happening
    (observed live on ``nonagentic-gtm`` and ``n8n-dev``, both two subjects, both
    perpetual). fr's own ``src/services/licenses.ts`` already treated that same
    conflict as ENTITLED, so fr and the gateway enforcer disagreed on identical
    input — the gateway was the one that was wrong.

    Which seat is returned: ``subject_id`` if supplied and resolvable, else the
    deterministic ``ACTIVE_LICENSE_PICK_ORDER`` winner (models.py). ``ambiguous``
    / ``candidate_count`` / ``subject_match`` report the condition to the caller;
    the WARN log fires only when the fallback had to GUESS between >1 candidate
    (warning merely because >1 license exists would page on success).

    This is a behaviour-preserving WIDENING that removes a live 503. It does NOT
    tighten who is admitted: a caller whose tenant holds a license but who holds
    none of its own keeps working exactly as before.

    Read-only entitlement lookup: requires an internal-service role OR the
    least-privilege read role ``license-reader`` (see ``require_internal_or_reader``).
    The broader /internal/licenses* listing endpoints remain internal-service only.

    AUTHORIZATION (cross-tenant IDOR closed 2026-08-01): ``tenant_id`` is a
    caller-controlled QUERY parameter, so ``require_internal_or_reader`` alone
    authorizes NOTHING about *whose* entitlement is returned. Every opaque
    license token self-introspects to exactly ``["license-reader"]``
    (auth/middleware.py ``LICENSE_TOKEN_ROLES``), so before this guard any token
    BEARER could read another tenant's license_id / subject_id / plan / metadata
    via ``?tenant_id=<other-tenant>``. Two tiers, mirroring the other scoped
    read routes (``list_user_licenses`` / ``get_tenant``):
      - an INTERNAL-SERVICE caller (``service-internal`` / ``onboarding-internal``
        / ``license-admin``) is the legitimate cross-tenant S2S entitlement
        lookup surface — no tenant filter.
      - everyone else (a bare ``license-reader``, INCLUDING a trusted
        multi-tenant proxy acting for one tenant) may resolve ONLY its own
        authoritative tenant: the one the middleware already reconciled onto
        ``request.state.user`` (the X-Tenant-Id header is honored there only for
        admins / trusted proxies), never the raw query value.

    That guard lives in the shared ``_enforce_own_tenant_scope`` helper — the
    two tiers described above are unchanged, they are just not written out
    inline where a future second copy could quietly become weaker.
    """
    await _enforce_own_tenant_scope(
        request,
        user=user,
        tenant_id=tenant_id,
        delegated_roles=INTERNAL_SERVICE_ROLES,
        detail="Not authorized to read another tenant's active license",
    )
    if realm is not None:
        # Migration 072: license-authz resolves a non-portal realm user's
        # entitlement S2S and names the user's realm. Applied AFTER the caller's
        # own authorization (unchanged), so the parameter can only narrow.
        await asyncio.to_thread(_enforce_forwarded_realm_owner, realm, tenant_id)
    return await _resolve_active_entitlement(
        tenant_id=tenant_id,
        app_key=app_key,
        subject_id=subject_id,
        ctx="get_active_license",
    )


@app.post(
    "/license/revoke",
    response_model=TokenRevokeResponse,
    operation_id="revoke_token",
    tags=["tokens"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "A caller other than the cross_owner break-glass asked to revoke a "
                "DEPENDENCY licence (is_dependency) that is not releasable now; "
                "`detail.code` (= `detail.error`) is `dependency_still_required` — a remaining active licence on the tenant "
                "still requires its app through app_requires, listed in "
                "`detail.required_by` as {license_id, tenant_id, app_key} (revoke that "
                "licence instead; the dependency is then released automatically); or "
                "`ensure_in_progress` — a bundle ensure "
                "for the tenant holds its lock, retry. Nothing was revoked."
            ),
        },
    },
)
async def revoke_token_or_license(
    request: TokenRevokeRequest,
    tenant_id: str = Depends(require_tenant_id),
    user: User = Depends(require_any_role("license-admin", "license-manager")),
):
    """
    Revoke a token or all tokens for a license

    Requires 'license-admin' or 'license-manager' role
    Verifies tenant ownership before revocation
    """
    try:
        if request.token:
            # Migration 050: the token's app must be issuable by the caller.
            await asyncio.to_thread(_authorize_token_revoke, user, request.token, tenant_id)
            # Resolve ownership and revoke in ONE transaction.
            #
            # ⚠️ This used to gate the write on ``introspect_token(...)["active"]``,
            # and that gate was the defect. ``active`` is LICENSE-aware, so a
            # LEAKED token on a lapsed license answered "Token not found or
            # already invalid" and wrote NOTHING. Nothing in this service
            # transitions a token when it expires, and ``Token.is_valid`` reads
            # only ``revoked`` plus the token's own expiry — so on renewal the
            # credential the operator believed dead came back live at full
            # entitlement. Revocation is an administrative act on a row, not a
            # statement about whether the token authenticates right now.
            #
            # Splitting ownership and write across two sessions would also be
            # TOCTOU; ``revoke_token_for_tenant`` does both in one.
            #
            # Off the event loop for the same reason /license/introspect is (see
            # the comment on that handler): this is synchronous, blocking DB I/O,
            # and running it inline in an ``async def`` stalls EVERY request this
            # worker is serving -- including /health/live and /health/ready. That
            # duration is unbounded: the engine sets no connect_timeout, so a
            # pgbouncer IP that blackholes (drops packets without a RST) hangs
            # until the kernel gives up on SYN retransmits. A stalled worker fails
            # its probes and k8s restarts the pod, turning one admin revoke into a
            # pod bounce.
            # ``revoke_token_for_tenant`` returns (outcome, revoked_count). The
            # count exists because a raw token is NOT unique in the schema — the
            # same hash can sit on several licences of the same tenant, and the
            # pre-fix code killed exactly one of them while telling the operator
            # the credential was dead. When more than one row is written, say so:
            # an incident responder needs to see that this credential was
            # duplicated, not just that "it worked".
            outcome, revoked_count = await asyncio.to_thread(
                db_manager.revoke_token_for_tenant, request.token, tenant_id
            )
            if outcome == "revoked":
                if revoked_count > 1:
                    return TokenRevokeResponse(
                        revoked=True,
                        message=(
                            f"Token revoked successfully ({revoked_count} token records "
                            f"shared this token; all were revoked)"
                        ),
                    )
                return TokenRevokeResponse(revoked=True, message="Token revoked successfully")
            if outcome == "already_revoked":
                # Idempotent: the caller asked for a state the row is already in,
                # so this is a success. Answering False here made a retry
                # indistinguishable from a failure -- and identical to the
                # wrong-tenant and unknown-token answers -- while the sibling
                # ``license_id`` arm below has always been idempotent.
                return TokenRevokeResponse(revoked=True, message="Token was already revoked")
            # "not_found" covers BOTH "no such token" and "not your tenant", on
            # purpose: one message, so this endpoint cannot be used to probe for
            # the existence of a token in another tenant.
            return TokenRevokeResponse(revoked=False, message="Token not found or already invalid")

        elif request.license_id:
            # Verify tenant owns the license before revoking
            license = await asyncio.to_thread(db_manager.get_license, UUID(request.license_id))
            if not license:
                return TokenRevokeResponse(revoked=False, message="License not found")
            # tenant_id is a UUID column since migration 016; str() it to compare
            # against the string header value (UUID != str is always True).
            if str(license.tenant_id) != tenant_id:
                # Don't reveal license exists - return same message as not found
                return TokenRevokeResponse(revoked=False, message="License not found")

            # Issuer rule, or the tenant owner revoking a bundle-minted licence
            # (``_bundle_owner_may_revoke``; archived tenants included) — or, here
            # only, a DEPENDENCY licence on its tenant that nothing still requires.
            await asyncio.to_thread(
                _authorize_licence_revoke,
                user,
                [license],
                tenant_id,
                "licence_revoke",
                allow_dependency=True,
            )
            # A DEPENDENCY licence still required on its tenant is refused 409 for
            # EVERY caller — issuer and owner alike — except the cross_owner
            # break-glass, which may force it (logged ``dependency_force_revoked``).
            refuse_required = bool(license.is_dependency) and not user.cross_owner

            # Revoke + release the tenant's now-orphaned dependency licences, in ONE
            # transaction (``revoke_license_releasing_dependencies``).
            outcome, released, required_by = await asyncio.to_thread(
                db_manager.revoke_license_releasing_dependencies,
                UUID(request.license_id),
                actor=str(user.sub or user.azp),
                refuse_required_dependency=refuse_required,
            )
            if outcome == "dependency_still_required":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "dependency_still_required",
                        # ``error`` mirrors ``code``: this service's 409 discriminator
                        # key everywhere else (test_409_discriminators_are_published).
                        "error": "dependency_still_required",
                        "license_id": str(request.license_id),
                        "required_by": required_by,
                        "message": (
                            "a remaining active licence on this tenant still requires this "
                            "dependency; revoke that licence and the dependency is released"
                        ),
                    },
                )
            if outcome == "ensure_in_progress":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "ensure_in_progress",
                        "error": "ensure_in_progress",
                        "license_id": str(request.license_id),
                        "message": "a bundle ensure for this tenant is running; retry",
                    },
                )
            success = outcome == "revoked"
            if success:
                # Tear down billing's mirrored quota config for this licence.
                #
                # ⛔ SPAWNED, NOT AWAITED, and that is the contract — not an
                # optimisation. Revoking is the security-critical half; billing's
                # mirror is derived state. Awaiting it would let a billing outage
                # (or its ~11s worst case: two 5s attempts plus the retry sleep)
                # delay — and, if anyone later "helpfully" propagated the failure
                # — VETO the revocation of a compromised licence. Failing closed
                # on derived state is the wrong trade here, so the write above is
                # already committed and this verdict has no path back to the
                # caller.
                #
                # MUST go through _spawn_background_task for the same reason
                # _provision_budget does at mint: a bare asyncio.create_task whose
                # result is discarded is only WEAKLY referenced by the loop and can
                # be garbage-collected mid-flight.
                #
                # This is a FAST PATH, not a replacement for billing's
                # delete_absent_quota_configs sweep — that reconciler stays the
                # backstop and is what a QUOTA_CONFIG_TEARDOWN_FAILED line tells
                # the operator to reach for.
                #
                # tenant_id is the header-derived value the ownership check three
                # lines above already proved owns this licence, so this cannot
                # send billing a tenant the caller does not hold.
                _spawn_background_task(
                    _teardown_quota_config(
                        license_id=str(request.license_id),
                        tenant_id=tenant_id,
                    )
                )

                # And the SPEND half. Same contract, same reasons, deliberately a
                # SEPARATE task: a quota config caps request RATE, a token budget
                # caps SPEND, and one failing must not suppress the other. Spawned
                # independently so a billing refusal on the quota route still
                # leaves the budget teardown its own attempt and its own marker.
                #
                # ⭐ This is the half that was missing. MEASURED 2026-09-09: 19
                # active `license_sync` budgets — 430M tokens — were still backing
                # REVOKED licences, because nothing on the revoke path ever told
                # billing, and billing's own reconciler runs on no timer.
                _spawn_background_task(
                    _teardown_token_budget(
                        license_id=str(request.license_id),
                        tenant_id=tenant_id,
                    )
                )
                # The released dependency licences owe the same two teardowns.
                for dep_id in released:
                    _spawn_background_task(
                        _teardown_quota_config(license_id=dep_id, tenant_id=tenant_id)
                    )
                    _spawn_background_task(
                        _teardown_token_budget(license_id=dep_id, tenant_id=tenant_id)
                    )
                message = "License and all tokens revoked successfully"
                if released:
                    message += (
                        f"; {len(released)} dependency licence(s) no longer required were released"
                    )
                return TokenRevokeResponse(
                    revoked=True, message=message, released_dependency_ids=released
                )
            else:
                return TokenRevokeResponse(revoked=False, message="License not found")

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'token' or 'license_id' must be provided",
            )

    except HTTPException:
        raise
    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "revoke_token_or_license: Invalid UUID"
        ) from e
    # Top-level handler: spans DB + tenant-ownership checks; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Revocation failed", e, "Revocation failed"
        ) from e


# ============= License Endpoints =============


@app.get(
    "/licenses/user/{subject_id}",
    response_model=list[LicenseResponse],
    operation_id="list_user_licenses",
    tags=["licenses"],
    responses=COMMON_ERROR_RESPONSES,
)
async def list_user_licenses(
    subject_id: str,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    Get all licenses for a specific subject/user

    Requires 'license-admin' or 'license-reader' role.

    AUTHORIZATION (cross-tenant IDOR closed 2026-07-27):
    ``subject_id`` is a caller-controlled path parameter, so the role check
    alone authorizes NOTHING about *whose* licenses are returned. Three tiers:

    - ``license-admin`` — the cross-tenant operator surface (same contract as
      ``GET /licenses/admin/all``). Any subject, no tenant filter. This is what
      portal-bff and provisioning hold (see auth/config.py:97).
    - a known owning app (verified ``azp`` in ``api_client``, migration 050 —
      e.g. fr-backend / w2v-backend) — acts on
      behalf of an end user, so it may ask for a subject that is not itself,
      but the result is SCOPED TO THE TENANT it is acting for. That is the
      cross-tenant half of the hole, and it is the half that mattered: the
      w2v license gate legitimately calls this route with only
      ``license-reader``/``license-manager`` (no ``license-admin``), so a flat
      caller==subject rule would have broken it.
    - everyone else — own subject only, tenant-filtered.
    """
    try:
        # Every db_manager call in this handler is offloaded to a worker thread —
        # see the comment on ``introspect_token``. This handler stays ``async def``
        # because it awaits ``get_tenant_id``, so FastAPI will not move it to the
        # threadpool for us.
        # Try keycloak_sub first (most common case - portal passes keycloak sub)
        subject = await asyncio.to_thread(db_manager.get_subject_by_keycloak_sub, subject_id)

        # If not found, try as internal UUID
        if not subject:
            try:
                subject = await asyncio.to_thread(db_manager.get_subject, UUID(subject_id))
            except ValueError:
                pass  # Not a valid UUID format

        if not subject:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

        tenant_filter: str | None = None
        if not user.has_realm_role("license-admin"):
            # Everything below the operator tier is tenant-scoped. The acted-on
            # tenant was already reconciled by the auth middleware (a client
            # X-Tenant-Id is honored only for a tenant the caller's api_client
            # owner owns or holds a grant on; cross_owner: any, audited), so
            # this is not header trust.
            tenant_filter = await get_tenant_id(request)

            # Migration 050: a known owning app (api_client) acts on behalf of
            # end users and may ask for another subject; the result stays scoped
            # to the tenant it acts for, which the middleware admitted only if
            # the app owns it or holds a grant on it.
            await asyncio.to_thread(_ensure_owner_resolved, user)
            if user.owner_key is None and not user.cross_owner:
                # Ordinary caller: it must be asking for ITSELF. ``subject_id``
                # may arrive as either the keycloak_sub or the internal UUID, so
                # compare against the RESOLVED subject's keycloak_sub — that is
                # what the verified token's ``sub`` is.
                if str(subject.keycloak_sub) != str(user.sub):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Not authorized to read another subject's licenses",
                    )

        licenses = await asyncio.to_thread(
            db_manager.list_licenses, subject_id=UUID(str(subject.id)), tenant_id=tenant_filter
        )

        # Pre-fetch all apps to avoid N+1 query
        app_ids = {UUID(str(lic.app_id)) for lic in licenses if lic.app_id}
        all_apps = await asyncio.to_thread(db_manager.list_apps)
        apps_by_id = {UUID(str(app.id)): app for app in all_apps if UUID(str(app.id)) in app_ids}

        # Enrich licenses with app info for frontend display
        result = []
        for lic in licenses:
            lic_dict = lic.to_dict()
            # Fetch app info from pre-loaded lookup
            app = apps_by_id.get(UUID(str(lic.app_id))) if lic.app_id else None
            if app:
                lic_dict["app"] = AppInfo(id=str(app.id), key=str(app.key), name=str(app.name))
            result.append(LicenseResponse(**lic_dict))

        return result

    except HTTPException:
        raise
    # Top-level handler: spans DB + UUID parse + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list licenses",
            e,
            "Failed to list licenses",
        ) from e


class RegenerateTokenResponse(BaseModel):
    """Response for regenerate token endpoint"""

    token: str
    message: str = "Save this token - it won't be shown again"
    # The replacement INHERITS its expiry from the tokens it replaces, so the
    # caller does not choose it and could not previously observe it either — this
    # response carried the raw token and nothing else. A rotated credential whose
    # lifetime is invisible is a credential nobody can plan around; `null` means
    # perpetual, matching the mint route's `expires_days` contract.
    expires_at: datetime | None = Field(
        None, description="When the new token expires; null means perpetual (until revoked)."
    )


def _license_state_detail(license_id: object, license_obj: License) -> dict[str, Any]:
    """The state fields of a 409 ``license_not_active`` body, shared by BOTH token
    routes (``regenerate-token`` and ``POST /licenses/{id}/tokens``).

    fr's console reads this shape off ``regenerate-token``; one builder keeps the
    two routes from drifting. The ``"error"`` key itself stays LITERAL at each
    raise site on purpose: ``tests/unit/test_409_discriminators_are_published.py``
    only sees discriminators written inline in the handler.
    """
    return {
        "license_id": str(license_id),
        "status": license_obj.effective_status(),
        "stored_status": str(license_obj.status),
        "starts_at": license_obj.starts_at.isoformat() if license_obj.starts_at else None,
        "expires_at": license_obj.expires_at.isoformat() if license_obj.expires_at else None,
    }


def _refuse_dependency_licence(license_obj: Any, action: str) -> None:
    """403 ``dependency_licence``: no USER token on a dependency licence (migration 059).

    A dependency licence exists only because a bundled app REQUIRES the app
    (``app_requires``); the services that call it mint short-lived tokens through
    ``POST /internal/tenants/{id}/bundle-token``. Handing the tenant a token of its
    own would let it call the dependency directly, which is the thing it did not buy.
    Checked after ownership and issuance, before any write, so nothing is revoked or
    minted.
    """
    if getattr(license_obj, "is_dependency", False):
        logger.warning(
            "Refusing %s on dependency licence %s (app %s): bundle-token only",
            action,
            license_obj.id,
            license_obj.app_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "dependency_licence",
                "license_id": str(license_obj.id),
                "remedy": (
                    "a dependency licence is internal-only: services mint it via "
                    "POST /internal/tenants/{tenant_id}/bundle-token"
                ),
            },
        )


@app.post(
    "/licenses/{license_id}/regenerate-token",
    response_model=RegenerateTokenResponse,
    operation_id="regenerate_license_token",
    tags=["tokens"],
    responses={
        **COMMON_ERROR_RESPONSES,
        403: {
            "model": ErrorResponse,
            "description": (
                "Forbidden, nothing revoked or minted: a missing role, the caller may not "
                "issue this app, or `detail.error` is `dependency_licence` — the licence "
                "exists only because a bundled app requires this app (migration 059) and "
                "is minted through the internal bundle-token route only."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing was revoked and no token was minted. Branch on `detail.error`, "
                "which is one of exactly three values:\n\n"
                "- `license_not_active` — the license is not effectively active "
                "(expired / revoked / not yet started). `detail` also carries `status`, "
                "`stored_status`, `starts_at` and `expires_at`. Renew or investigate the "
                "license; retrying the rotation cannot succeed.\n"
                "- `all_tokens_expired` — every unrevoked token on the license has already "
                "expired, so there is no live expiry to carry forward and a rotation would "
                "mint a credential that is already dead. Mint one explicitly via "
                "POST /licenses/{license_id}/tokens.\n\n"
                "- `all_tokens_revoked` \u2014 the license HAS tokens but every one of them is "
                "revoked, so there is no live expiry to carry forward and rotating would mint an "
                "UNBOUNDED replacement for a credential that was deliberately killed (the sharpest "
                "case: revoke a leaked 30-day token, press regenerate, receive one that never "
                "expires). Mint one explicitly via POST /licenses/{license_id}/tokens, where the "
                "expiry is an argument.\n\n"
                "The three need different handling, so a client that cannot tell them apart "
                "will strand the caller on one of them."
            ),
        },
    },
)
def regenerate_token(
    license_id: UUID,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-manager")),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Regenerate a token for a license.

    Revokes all existing tokens and creates a new one.
    The new token is only returned once - store it securely.

    Requires 'license-admin' or 'license-manager' role.

    AUTHORIZATION (read-role destructive rotation closed 2026-07-27): this
    accepted ``license-reader``, a READ role, for an operation that REVOKES
    every existing token for the license and mints a replacement. That is a
    destructive credential rotation, and the blast radius was wider than
    "your own token": the ownership check below is TENANT-level, not
    license-level, so any reader could rotate the tokens of any *sibling*
    license in its tenant — silently invalidating another workload's
    credentials (perpetual license tokens have ``expires_at = NULL``, so the
    victim sees an inexplicably-invalid token, not an expired one).

    Critically, an opaque license token self-introspects to exactly
    ``["license-reader"]`` (auth/middleware.py ``LICENSE_TOKEN_ROLES``), so
    before this change a mere token BEARER could call this. It now requires a
    Keycloak JWT with a write role — no opaque token can reach it at all.

    Matches the sibling mint route ``POST /licenses/{license_id}/tokens``,
    which already required ``license-admin``/``license-manager``.

    VALIDITY (unrecoverable destruction on a dead license closed 2026-08-08):
    this checked 404 and tenant ownership and then rotated — with NO validity
    check at all. Rotating on an expired / revoked / not-yet-started license
    therefore REVOKED the caller's still-working tokens and returned a
    replacement that ``/license/introspect`` immediately rejects as
    ``license_inactive``. Raw tokens are shown ONCE and only the SHA-256 hash is
    stored, so the destroyed credentials cannot be recovered — the caller is left
    with strictly less than it started with and no way back.

    The gate below runs BEFORE the destructive call and returns 409 CONFLICT,
    not 403: the caller is authorized, the RESOURCE is in the wrong state, and
    the fix (renew the license) is available to them. The structured ``detail``
    carries the EFFECTIVE status so fr's console
    (``fr/backend/src/routes/me/licenses.ts`` -> ``licensesClient.regenerateToken``)
    can say "expired" vs "revoked" without re-deriving the rule.

    EXPIRY: the replacement INHERITS the expiry of the tokens it replaces (see
    ``DatabaseManager.regenerate_token_for_license``). This route used to pass no
    ``expires_at``, so every rotation minted a PERPETUAL token — pressing
    "regenerate" on a 1-day token silently produced a never-expiring credential.
    A rotation can now never lengthen a credential's life, and a genuinely
    perpetual token stays perpetual. Orthogonal to the validity gate below: a
    VALID license could still launder a short-lived token this way.
    """
    try:
        # Get the license
        license_obj = db_manager.get_license(license_id)
        if not license_obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")

        # Verify ownership via tenant
        if str(license_obj.tenant_id) != tenant_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your license")

        _authorize_issuance(user, license_obj.app_id, tenant_id, "token_regenerate")
        _refuse_dependency_licence(license_obj, "token_regenerate")

        # CHECK BEFORE DESTROY. regenerate_token_for_license is an irreversible
        # bulk-revoke; it must not run for a license whose replacement token
        # would be dead on arrival. is_active() is the same predicate token
        # introspection uses — deliberately reused, not re-derived.
        if not license_obj.is_active():
            effective = license_obj.effective_status()
            logger.warning(
                "Refusing to regenerate token for license %s: effective status is %s "
                "(stored %s) — rotation would destroy working tokens and mint a dead one",
                license_id,
                effective,
                license_obj.status,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "license_not_active",
                    **_license_state_detail(license_id, license_obj),
                },
            )

        # Atomically revoke all existing tokens AND mint the replacement in ONE
        # transaction. Previously these were two separate commits, so a crash
        # between them left the license with zero tokens — and perpetual license
        # tokens (expires_at = NULL) are never auto-replaced, so that is a
        # permanent token_not_found, not a transient window. One commit means a
        # failure rolls both back and the old tokens survive.
        try:
            revoked_count, token_obj, raw_token = db_manager.regenerate_token_for_license(
                license_id
            )
        except AllTokensRevokedError as exc:
            # THIRD gate, and the one that is reachable by ordinary API use: the
            # license is active and its tokens have NOT expired — they have all
            # been REVOKED. Rotating would fall into the "nothing replaced" arm and
            # mint a PERPETUAL replacement, so an operator revoking a leaked 30-day
            # token and pressing regenerate would be handed a credential that never
            # expires. Nothing is destroyed; the remedy is an explicit mint.
            logger.warning(
                "Refusing to regenerate token for license %s: all %d token(s) on it are "
                "revoked — rotation would mint an UNBOUNDED replacement",
                license_id,
                exc.revoked_count,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "all_tokens_revoked",
                    "license_id": str(license_id),
                    "remedy": "mint a new token via POST /licenses/{license_id}/tokens",
                },
            ) from exc
        except AllTokensExpiredError as exc:
            # SECOND "check before destroy" gate, and the one the license-scoped
            # check above cannot see: this license IS effectively active, but every
            # unrevoked token on it has already expired, so there is no live expiry
            # to carry forward. The DAL refused rather than inherit a past one (which
            # would mint a token `/license/introspect` rejects immediately) or fall
            # into the perpetual arm (which would launder a dead credential into an
            # unbounded one). Nothing was revoked.
            #
            # `exc.newest_expiry` is operator diagnostics and stays in the LOG. It is
            # deliberately not echoed into `detail=` — `tests/unit/test_no_exception_leak.py`
            # keeps caught-exception state out of response bodies, and the caller's
            # next step does not depend on the timestamp.
            logger.warning(
                "Refusing to regenerate token for license %s: every unrevoked token has "
                "expired (most recent expiry %s) — rotation would destroy them and mint a "
                "dead replacement",
                license_id,
                exc.newest_expiry,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "all_tokens_expired",
                    "license_id": str(license_id),
                    "remedy": "mint a new token via POST /licenses/{license_id}/tokens",
                },
            ) from exc

        logger.info(
            f"Regenerated token for license {license_id} (revoked {revoked_count} prior tokens)"
        )

        return RegenerateTokenResponse(
            token=raw_token, expires_at=cast("datetime | None", token_obj.expires_at)
        )

    except HTTPException:
        raise
    # Top-level handler: spans DB + token mint; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to regenerate token",
            e,
            "Failed to regenerate token",
        ) from e


async def _state_subject_parentage(subject_pk: UUID, tenant_id: str, subject_ref: str) -> None:
    """Best-effort: record which org this subject belongs to (`subject.tenant_id`).

    ⛔ THIS CANNOT FAIL A LICENCE, and the ordering is the whole point. It runs
    only on a path that has ALREADY returned a licence to the caller's variable —
    never before the mint — so the ambiguity check in
    ``DatabaseManager.attribute_subject_to_tenant`` can see the licence that was
    just written. Every refusal reason and the rule behind it live on that method;
    this wrapper adds only "and never raise".

    Why here rather than in ``create_or_get_subject``: at subject creation the
    tenant is an UNVALIDATED header. By the time control reaches either call site
    below, the tenant has either passed the mint guard (a registered ``tenants``
    row) or is carrying an already-existing licence — and the method re-checks the
    registry itself regardless, so the idempotent path cannot smuggle an
    unregistered id in. That is why the money-bug shape (a Keycloak ``sub``
    forwarded as ``X-Tenant-Id``) cannot land here: it has no ``tenants`` row, so
    it is refused as ``tenant_unknown`` and logged, not written.
    """
    try:
        reason = await asyncio.to_thread(
            db_manager.attribute_subject_to_tenant,
            subject_id=subject_pk,
            tenant_id=tenant_id,
        )
    # Bare Exception on purpose: bookkeeping must never propagate into the licence
    # response. A DB blip here costs a log line, not a failed first login.
    except Exception:
        logger.warning(
            "Could not state subject parentage for %s (tenant %s); leaving tenant_id unchanged",
            subject_ref,
            tenant_id,
            exc_info=True,
        )
        return
    if reason == "set":
        logger.info("subject.tenant_id set for subject %s -> tenant %s", subject_ref, tenant_id)
    elif reason not in ("already_set", "no_licences"):
        # `ambiguous` / `tenant_is_machine` / `tenant_unknown` / `race_lost` are the
        # reasons an operator may want to see; `already_set` and `no_licences` are
        # the steady state and would be pure noise on every portal session.
        logger.info(
            "subject.tenant_id left unstated for subject %s (tenant %s): %s",
            subject_ref,
            tenant_id,
            reason,
        )


@app.post(
    "/licenses/user/{subject_id}",
    response_model=LicenseResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_user_license",
    tags=["licenses"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "No licence was created. Two cases:\n\n"
                "- `plan_revoked` (`detail.error`) — the requested plan is revoked "
                "(retired) and this tenant has never held a licence on it for this app, "
                "so it cannot join the plan. `detail` also carries `plan_id`. A tenant "
                "with ANY prior licence on the plan (active, revoked or expired) can "
                "still mint new licences on it, an active licence still reads back, "
                "and existing tokens keep minting and introspecting.\n"
                "- a string `detail` — the path `subject_id` is already a registered "
                "tenant id, so it cannot be a licence subject."
            ),
        },
    },
)
async def create_user_license(
    subject_id: str,
    license_data: LicenseCreate,
    tenant_id: str = Depends(require_tenant_id),
    user: User = Depends(require_any_role("license-admin", "license-manager")),
):
    """
    Create a new license for a subject/user

    Requires 'license-admin' or 'license-manager' role.
    tenant_id comes from the X-Tenant-Id header (ext_authz) or the token's
    tenant_id claim — NOT the subject. Minting a NEW license fails loud (400)
    unless tenant_id is a REGISTERED tenant, so neither a user id nor an
    unprovisioned tenant is ever persisted as a tenant (which would corrupt the
    license row and the provisioned billing budget). Idempotent re-provisioning
    of an already-existing license is unaffected (see the guard below).
    """
    try:
        # This handler CANNOT become a sync ``def``: it schedules the metering
        # background task via ``_spawn_background_task`` -> ``asyncio.create_task``,
        # which needs a running loop and would raise RuntimeError on every licence
        # creation from a worker thread. So it stays ``async def`` and each blocking
        # db_manager call is offloaded explicitly — see ``introspect_token``.
        # Get or create subject
        # The subject's realm: stated by the minting service when it can, else the
        # caller's realm (legacy). See `LicenseCreate.subject_realm` for why the
        # caller's realm is the wrong default for a service-minted human licence.
        # Migration 050: the caller's app must ISSUE this app and own (or hold a
        # grant on) the tenant — BEFORE anything is written or an existing
        # licence is returned.
        await asyncio.to_thread(
            _authorize_issuance, user, license_data.app_id, tenant_id, "licence_create"
        )
        caller_realm = user.iss.split("/realms/")[-1] if user.iss else REALM_NAME
        # ⛔ A stated non-human realm is honoured only for the EXACT subs it may name
        # (`cp-tenant` -> `cp-tenant:<this licence's tenant>:a|b`). A prefix check
        # alone let a caller mint unlimited unbilled `cp-tenant:<anything>` licences,
        # or state the realm for a person's sub and hide them from the seat count.
        realm_problem = stated_non_human_subject_problem(
            license_data.subject_realm, str(subject_id), str(tenant_id)
        )
        if realm_problem is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, realm_problem)
        keycloak_realm = license_data.subject_realm or caller_realm
        try:
            subject = await asyncio.to_thread(
                db_manager.create_or_get_subject,
                keycloak_sub=subject_id,
                keycloak_realm=keycloak_realm,
                # ⛔ THE SELF-TENANT MINT, STATED BY THE ONLY CALLER THAT CAN KNOW IT.
                # `create_or_get_subject` refuses a sub that is already a `tenants.id`
                # — but NOT on this path, where the caller is minting a licence whose
                # TENANT is that same id. That is the documented self-tenant shape
                # portal-bff drives (it forwards `sub` as `X-Tenant-Id` when the token
                # carries no `tenant_id` claim), it is what
                # `TestCreateUserLicenseMintGuard::test_guard_does_not_fire_for_real_self_tenant`
                # pins at 201, and refusing it would turn a billing-hygiene guard into
                # an outage on a live path.
                #
                # ⚠️ NOT PINNED TO ANY ID. It is derived from THIS call (`sub == tenant`)
                # and nothing else. `e148e8af-…` portal-render-e2e used to be the worked
                # example; migration 046 moved that TENANT to `01a1113b-…`, so subject
                # `e148e8af-…` now mints for a tenant that is NOT itself: this is False,
                # no exemption is needed (the sub is no longer a `tenants.id`, so the
                # guard has nothing to refuse), and `X-Tenant-Id: e148e8af-…` is refused
                # by the mint guard like any other unregistered id —
                # `TestCreateUserLicenseMintGuard::test_the_rekeyed_render_e2e_*`.
                #
                # ⚠️ Compared case-folded, NOT raw `==`: `subject_id` is a raw path
                # parameter and `tenant_id` comes from a header, so a case variant of
                # one id would otherwise read as two different ids and silently re-arm
                # the refusal on the very path this exempts — the same trap
                # `test_guard_fires_on_uuid_case_variant` records for the mint guard.
                # A non-UUID sub simply never matches, which is correct: it cannot be
                # a `tenants.id` either.
                self_tenant_mint=str(subject_id).strip().lower() == str(tenant_id).strip().lower(),
            )
        except ClientSafeError as exc:
            # ⛔ CAUGHT AT THIS CALL SITE, NOT HANDLER-WIDE — the rule the block at
            # the bottom of this handler states for itself ("catch THAT type at THAT
            # call site. A handler-wide clause cannot tell the caller's fault from
            # ours"). `subject_id` is a caller-supplied PATH PARAMETER, so this is
            # unambiguously the caller's fault.
            #
            # The only raise site is the principal guard in `create_or_get_subject`:
            # the supplied sub is already a registered `tenants.id`. 409, not 400 —
            # the request is well-formed and the conflict is with server state, the
            # same reading `POST /tenants/{id}` gives the mirror-image refusal.
            #
            # ⚠️ WITHOUT THIS CLAUSE the guard would still refuse, but the broad
            # `except Exception` below would have reported a deliberate caller-error
            # refusal as a 500 "Failed to create license" — hiding the message the
            # DAL authored for the caller AND putting a hygiene refusal into 5xx
            # alerting as a server fault.
            #
            # ⛔ EXCEPT a MalformedSubjectIdentifierError (a ClientSafeError subclass, so the
            # one safe-echo clause still covers it): a padded / control-char /
            # upper-case-UUID path id is malformed input -> 400. It is refused, never
            # normalised: a stripped '<A_sub>\n' IS A's sub, i.e. an alias.
            if isinstance(exc, MalformedSubjectIdentifierError):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if license_data.subject_realm and subject.keycloak_realm != license_data.subject_realm:
            # An EXISTING Subject keeps its realm — it is not rewritten from a request,
            # because two minting services disagreeing must not flip a person in and out
            # of the seat count. Say so, so the stale realm is findable and correctable.
            logger.warning(
                "subject_realm_mismatch: subject %s is stored in realm %r but the minting "
                "request stated %r; the stored realm was kept",
                subject.id,
                subject.keycloak_realm,
                license_data.subject_realm,
            )

        # Idempotency: at most one ACTIVE license per (tenant, app, subject).
        # portal-bff provisions on every session, so return the existing active
        # license instead of minting a duplicate. The partial unique index
        # uq_license_active_tenant_app_subject is the DB-level backstop.
        existing = await asyncio.to_thread(
            db_manager.find_active_license,
            tenant_id=tenant_id,
            app_id=license_data.app_id,
            subject_id=UUID(str(subject.id)),
        )
        if existing is not None:
            logger.info(
                f"Active license {existing.id} already exists for subject {subject_id} "
                f"app {license_data.app_id} tenant {tenant_id}; returning it (idempotent)"
            )
            # portal-bff POSTs on EVERY session, so this path — not the mint — is
            # where most subjects are seen. Attributing only on first mint would
            # leave every subject that predates this change NULL forever.
            await _state_subject_parentage(UUID(str(subject.id)), tenant_id, subject_id)
            return _enrich_license_response(existing)

        # Mint guard — a NEW license may only be minted for a REGISTERED tenant.
        # This pre-enforces the license.tenant_id -> tenants.id FK at the app layer
        # (Stage 1 of the FK campaign) and closes BOTH orphan sources at once:
        #   1. money-bug: a Keycloak `sub` (a user id) IS a valid UUID, and
        #      portal-bff forwards `sub` as X-Tenant-Id when the token has no
        #      tenant_id claim — so it sails past validate_tenant_uuid's shape
        #      check. Persisting it would corrupt licenses.tenant_id and the
        #      metering token_budgets.tenant_id.
        #   2. unprovisioned tenant: a real-but-unregistered tenant UUID (how the
        #      remaining orphans are minted, e.g. a provision_* run that never
        #      registered the tenant).
        # The tenants registry is authoritative — the caller must register the
        # tenant (POST /tenants, or ensure_tenant in the provision_* scripts)
        # BEFORE minting a license for it. get_tenant resolves by UUID or name.
        #
        # Placed AFTER the idempotency return on purpose: re-provisioning an
        # ALREADY-EXISTING (even orphan-tenant) license must keep working — the
        # guard only blocks the creation of NEW orphans, never a caller reading
        # back state that predates this guard (portal-bff POSTs every session).
        mint_tenant = await asyncio.to_thread(db_manager.get_tenant, tenant_id)
        if mint_tenant is None:
            # Canonical-UUID compare (subject_id is the raw path param) purely to
            # sharpen the log line for the money-bug case; the refusal is the same
            # either way.
            try:
                _is_user_id = UUID(str(tenant_id)) == UUID(str(subject_id))
            except ValueError, AttributeError, TypeError:
                _is_user_id = False
            logger.warning(
                "Refusing to mint license for subject %s: tenant_id %s is not a "
                "registered tenant (no tenants row)%s. Register the tenant first.",
                subject_id,
                tenant_id,
                " — and it equals subject_id, i.e. a user id sent as X-Tenant-Id"
                if _is_user_id
                else "",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "refusing to mint: tenant_id is not a registered tenant "
                    "(no tenants row) — register the tenant before creating a license"
                ),
            )

        # Second half of the mint guard: a REGISTERED tenant is not automatically a
        # tenant that may back a NEW licence. An ARCHIVED one may not.
        #
        # ``set_tenant_archived`` is this API's closest thing to a delete, and
        # ``list_tenants`` already filters archived rows out of the picker — so an
        # archived tenant is one an operator has retired. Minting against it
        # created a live, billable licence on a tenant that no longer appears in
        # any listing: invisible to the operator who retired it, and un-retiring
        # itself. 17 tenants were archived on 2026-09-04, so this is live, not
        # theoretical.
        #
        # ⛔ WHY THE GATE IS HERE AND NOT INSIDE ``get_tenant``.
        #
        # ``db_manager.get_tenant`` has 10 production call sites; exactly ONE
        # (this one) is a mint. Teaching the shared getter to hide archived rows
        # would silently break the other nine, and two of those breakages are
        # severe:
        #
        #   * ``auth/tenant.py`` resolves a tenant on the AUTHZ path for every
        #     request. Hiding the row would turn "retired from the picker" into
        #     "every in-flight request for that tenant starts failing" — the
        #     cut-off that ``TestArchivingRevokesNothing`` exists to forbid, and
        #     that ``test_the_tenant_itself_is_still_resolvable_by_id`` pins
        #     directly.
        #   * ``create_tenant``'s two existence checks (by id and by name) would
        #     stop seeing an archived row and happily mint a DUPLICATE tenant on
        #     top of it, breaking the unique-name invariant.
        #
        # And the read routes — ``GET /tenants/{name_or_id}``, the byo-mode
        # lookups, the caller's own-tenant read — are how an operator INSPECTS
        # what they just archived. Filtering there would make an archived tenant
        # un-inspectable through the very API that archived it.
        #
        # So the shared getter keeps its meaning ("resolve this row, whatever its
        # state") and the WRITE path states its own precondition. That is also
        # why archiving still revokes nothing: this blocks new licences only, and
        # the existing ones keep working exactly as before.
        if mint_tenant.archived_at is not None:
            logger.warning(
                "Refusing to mint license for subject %s: tenant %s is ARCHIVED (archived_at=%s). "
                "Un-archive the tenant before creating a license.",
                subject_id,
                tenant_id,
                mint_tenant.archived_at,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "refusing to mint: tenant is archived — un-archive the tenant "
                    "before creating a license"
                ),
            )

        # Third mint gate: no NEW TENANT on a REVOKED (retired) plan (#258).
        # Retiring a plan closes it to tenants with no history on it. A tenant
        # already on it is exempt: it keeps onboarding members, and CommonPractice
        # changes licence terms by revoking and then re-minting on the same plan,
        # so ANY prior licence row (revoked, expired) counts as "already on it".
        # Placed AFTER the idempotency return, so an existing active licence always
        # reads back; token mint / introspect never pass through here.
        # An unknown plan_id is NOT judged here: that is create_license's concern.
        mint_plan = await asyncio.to_thread(db_manager.get_plan, license_data.plan_id)
        if (
            mint_plan is not None
            and mint_plan.revoked
            and not await asyncio.to_thread(
                db_manager.tenant_has_licence_on_plan,
                tenant_id,
                license_data.app_id,
                license_data.plan_id,
            )
        ):
            logger.warning(
                "Refusing to mint license for subject %s: plan %s is revoked and "
                "tenant %s has never held it",
                subject_id,
                license_data.plan_id,
                tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "plan_revoked",
                    "plan_id": str(license_data.plan_id),
                    "message": (
                        "refusing to mint: the plan is revoked and this tenant is not on "
                        "it -- choose an active plan"
                    ),
                },
            )

        # No parsing: `LicenseCreate` declares these as `AwareDatetime`, so pydantic
        # has already produced tz-aware datetimes (or rejected the request with a
        # 422 naming the field). The `datetime.fromisoformat()` calls that used to
        # live here were the source of the naive-datetime write — see the field
        # definitions.
        starts_at = license_data.starts_at
        expires_at = license_data.expires_at

        # Create license with tenant_id from trusted header
        try:
            license = await asyncio.to_thread(
                db_manager.create_license,
                subject_id=UUID(str(subject.id)),
                app_id=license_data.app_id,
                plan_id=license_data.plan_id,
                tenant_id=tenant_id,
                region_id=license_data.region_id,
                starts_at=starts_at,
                expires_at=expires_at,
                license_metadata=license_data.metadata,
            )
        except IntegrityError:
            # Lost a race to a concurrent provision (unique index) — return the
            # active license that won instead of 500ing.
            existing = await asyncio.to_thread(
                db_manager.find_active_license,
                tenant_id=tenant_id,
                app_id=license_data.app_id,
                subject_id=UUID(str(subject.id)),
            )
            if existing is not None:
                return _enrich_license_response(existing)
            raise

        logger.info(f"License created: {license.id} for subject {subject_id} tenant {tenant_id}")

        # AFTER the commit above, never before: the licence just written is itself
        # the evidence the admission rule counts over.
        await _state_subject_parentage(UUID(str(subject.id)), tenant_id, subject_id)

        # Fire-and-forget: provision budget in metering service.
        # MUST go through _spawn_background_task — a bare asyncio.create_task
        # whose result is discarded is only WEAKLY referenced by the loop and can
        # be garbage-collected mid-flight, silently losing metering for a licence
        # that has already been handed back to the caller as 201.
        _spawn_background_task(
            _provision_budget(
                license_id=str(license.id),
                tenant_id=tenant_id,
                plan_id=str(license_data.plan_id),
                # The app is what makes the resulting quota-config row VISIBLE to
                # billing's licence resolver — see the payload comment in
                # ``_push_quota_config``. Taken from the request rather than
                # re-read: ``license_data.app_id`` is the same value the licence
                # row was just created with, two calls above.
                #
                # ⛔ ONLY the id is passed here. The NAME is looked up inside
                # ``_provision_budget`` via ``asyncio.to_thread``, because that
                # lookup is SQL and this call site is on the event loop — resolving
                # it here froze every other request on the pod for the duration of
                # the SELECT. Caught by test_no_sql_on_event_loop, not by review.
                app_id=str(license_data.app_id),
            )
        )

        return _enrich_license_response(license)

    except HTTPException:
        # Deliberate control-flow (e.g. the mint guard's 400) — let FastAPI
        # return it verbatim instead of the broad 500 wrapper below.
        raise
    # NO `except ValueError` here, deliberately — read this before re-adding one.
    #
    # This body used to be wrapped in `except ValueError -> 400 "Invalid input"`.
    # The clause was written for the `datetime.fromisoformat()` calls that once
    # parsed `starts_at`/`expires_at` here, but it spanned EVERYTHING: the four
    # `asyncio.to_thread` DAL calls, the `UUID(...)` conversions,
    # `_enrich_license_response` — whose `LicenseResponse(**data)` raises pydantic
    # `ValidationError`, which IS a `ValueError` — and the background-task
    # scheduling. Any server-side fault in that span was reported as 400: telling
    # the caller it sent bad input when it did not, and keeping a real defect out
    # of 5xx alerting entirely.
    #
    # #152 moved parsing to the `AwareDatetime` boundary, so there is no
    # caller-caused `ValueError` left on this path.
    #
    # ⚠️ CORRECTED 2026-09-23 — this comment used to read "`ClientSafeError` … is
    # not reachable either … Its DAL path — `create_or_get_subject`, … — contains
    # no `raise`". THAT IS NO LONGER TRUE: `create_or_get_subject` now raises
    # `ClientSafeError` when the supplied `keycloak_sub` is already a registered
    # `tenants.id` (the second door onto the tenant/subject invariant). The
    # remaining DAL calls — `find_active_license`, `get_tenant`, `create_license`
    # — still contain no `raise`, and `get_tenant` still handles its own `UUID()`
    # `ValueError` internally.
    #
    # That one reachable raise is caught AT ITS CALL SITE above and returned as a
    # 409, exactly as the next paragraph prescribes — so this handler still has no
    # handler-wide `ValueError` clause, and must not gain one. The correction is
    # recorded rather than silently edited because the old sentence was load-bearing:
    # a reader who trusted it would conclude no call site needed guarding, and the
    # refusal would surface as a 500 with the caller-safe message swallowed.
    #
    # If a future change puts another genuinely caller-caused `ValueError` on this
    # path, catch THAT type at THAT call site. A handler-wide clause cannot tell the
    # caller's fault from ours.
    #
    # Top-level handler: spans the DB calls and background-task scheduling; any
    # unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create license",
            e,
            "Failed to create license",
        ) from e


@app.get(
    "/licenses",
    response_model=list[LicenseResponse],
    operation_id="list_licenses",
    tags=["licenses"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_licenses(
    status_filter: str | None = Query(None, alias="status"),
    app_id: str | None = Query(None),
    plan_id: str | None = Query(None),
    subject_id: str | None = Query(None),
    tenant_id: str = Depends(get_tenant_id),
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all licenses with optional filters

    Requires 'license-admin' or 'license-reader' role
    Filters by tenant_id from X-Tenant-Id header (or JWT sub for internal calls)
    """
    try:
        # Convert string IDs to UUIDs
        subject_uuid = UUID(subject_id) if subject_id else None
        app_uuid = UUID(app_id) if app_id else None
        plan_uuid = UUID(plan_id) if plan_id else None

        licenses = db_manager.list_licenses(
            subject_id=subject_uuid,
            app_id=app_uuid,
            plan_id=plan_uuid,
            status=status_filter,
            tenant_id=tenant_id,
        )

        return [_enrich_license_response(license) for license in licenses]

    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "list_licenses: Invalid UUID"
        ) from e
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list licenses",
            e,
            "Failed to list licenses",
        ) from e


@app.get(
    "/licenses/admin/all",
    response_model=list[LicenseResponse],
    operation_id="list_all_licenses_admin",
    tags=["licenses"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_all_licenses_admin(
    status_filter: str | None = Query(None, alias="status"),
    include_deleted: bool = Query(
        False,
        description=(
            "Include soft-deleted licences (`deleted_at` set, migration 036). `false` "
            "(default) matches what this endpoint returned before `LICENSE_SOFT_DELETE` "
            "was armed — a deleted licence is simply absent. `true` is the AUDIT view: "
            "the retained row IS this service's record of the deletion (there is no audit "
            "table), so `deleted_at` / `deleted_by` / `delete_reason` are only readable "
            "through it."
        ),
    ),
    user: User = Depends(require_any_role("license-admin")),
):
    """
    List licenses across ALL tenants. Requires `license-admin` role.

    Deliberately bypasses tenant_id filtering — caller is the platform-admin
    surface on portal-bff acting as a service-account on behalf of a
    cross-tenant operator. Per-tenant listing remains at GET /licenses.

    ⚠️ `include_deleted=true` is the ONLY way to read a deleted licence's audit
    stamp. This is the cross-tenant operator surface, which is where an
    investigation into "who deleted this and why" actually starts; the per-tenant
    `GET /licenses` deliberately does not offer it, because a tenant-scoped caller
    is asking what it HAS, not what it once had.
    """
    try:
        licenses = db_manager.list_licenses(
            subject_id=None,
            app_id=None,
            plan_id=None,
            status=status_filter,
            tenant_id=None,
            include_deleted=include_deleted,
        )
        return [_enrich_license_response(license) for license in licenses]
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list all licenses",
            e,
            "Failed to list all licenses (admin)",
        ) from e


class LicenseBudgetUpdate(BaseModel):
    """Set or clear one licence's own monthly token budget (migration 041)."""

    # REQUIRED, deliberately: ``null`` CLEARS the override, so an omitted key
    # must not be indistinguishable from an explicit clear. 0 is a VALUE
    # (block all), not "unset".
    token_budget_override: int | None = Field(
        ...,
        ge=0,
        description=(
            "This licence's own monthly LLM token budget, overriding the plan's "
            "`token_budget_monthly`; 0 = block all. `null` CLEARS the override so "
            "the licence returns to the plan's value. Required — omitting the key "
            "is a 422, never a clear."
        ),
    )
    reason: str | None = Field(
        None, description="Why the budget changed. Recorded in the audit log line."
    )


@app.patch(
    "/licenses/{license_id}/budget",
    response_model=LicenseResponse,
    operation_id="set_license_budget",
    tags=["licenses"],
    responses={
        **COMMON_ERROR_RESPONSES,
        403: {
            "model": ErrorResponse,
            "description": (
                "Forbidden; nothing written. The caller lacks `license-admin`, or "
                "`detail.error` is `unknown_api_client` (the client is not a "
                "registered owning app), `app_not_issuable_by_caller` (the caller's "
                "owner does not issue this licence's app), or `tenant_not_accessible` "
                "(it neither owns the tenant nor holds an ACTIVE `tenant_app_grant` "
                "on it). `cross_owner` callers bypass the last two, audited."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "`License not found` — no licence with that id. Nothing written.",
        },
    },
)
def set_license_budget(
    license_id: UUID,
    body: LicenseBudgetUpdate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role("license-admin")),
):
    """Set or clear a licence's own monthly token budget (`token_budget_override`).

    Requires 'license-admin' AND the licence-write rule (`_authorize_issuance`):
    the caller's owner must ISSUE the licence's app AND own the tenant or hold an
    ACTIVE `tenant_app_grant` on it; `cross_owner` bypasses, audited.

    ⚠️ THE GUARD IS `_authorize_issuance`, NOT `_resolve_owned_tenant` —
    deliberately, and do NOT "tighten" it to the latter. `_resolve_owned_tenant`
    protects TENANT-WIDE settings (rename, archive, byo-mode, grants) and answers
    every GRANTEE 403 `tenant_owner_only`. fr holds a GRANT on conveyor, never
    ownership, so routing this route through it would ship a console control fr
    cannot call — the defect this whole change exists to remove. As a licence
    action this follows the same rule as minting/revoking a licence there.
    Measured live 2026-10-31: conveyor's licence
    ``01a12af4-7bd2-7165-bddf-069fc61096b1`` is app ``inference-router``, whose
    ``app_issuer`` set is {fr, commonpractice} (migration 050), and fr ALREADY
    holds an ACTIVE ``tenant_app_grant`` on the tenant (``granted_by =
    'migration-050'``, rule 2), so fr qualifies on BOTH arms today. No migration
    is needed for this route; the listing was the only thing hiding the tenant.

    `token_budget_override=null` clears the override and returns the licence to
    the plan's budget. `reason` is optional and lands on the audit line.
    This service has no audit table; the log line IS the record, same mechanism
    as `budget_currency_changed`.

    The column is written and COMMITTED before the push, and the billing push
    runs after the response via `push_effective_token_budgets` — the same
    failure contract as `PATCH /plans/{id}`: a billing failure never fails this
    request, and the metering reconcile sweep repairs the difference. Without
    that push an edit would sit on the licence while billing (and Redis, which
    license-authz reads) kept enforcing the old cap until a sweep ran.
    """
    try:
        license = db_manager.get_license(license_id)
        if license is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")
        _authorize_issuance(user, license.app_id, license.tenant_id, "license_budget_set")
        previous = license.token_budget_override
        updated = db_manager.set_license_token_budget_override(
            license_id, body.token_budget_override
        )
        if updated is None:  # raced a hard delete between the two reads
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")
        logger.info(
            "license_budget_override_changed: license=%s tenant=%s from=%s to=%s "
            "reason=%s actor=%s azp=%s realm=%s at=%s",
            updated.id,
            updated.tenant_id,
            previous,
            body.token_budget_override,
            body.reason,
            user.sub,
            user.azp,
            user.realm,
            datetime.now(UTC).isoformat(),
        )
        # After the commit and after the response (BackgroundTasks). Same
        # contract as update_plan: never fails this request.
        background_tasks.add_task(
            push_effective_token_budgets,
            license_ids=[str(updated.id)],
            reason=(
                f"license {updated.id} token_budget_override "
                f"{previous} -> {body.token_budget_override}"
                + (f" ({body.reason})" if body.reason else "")
            ),
        )
        return _enrich_license_response(updated)
    except HTTPException:
        raise
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to set license budget",
            e,
            "Failed to set license budget",
        ) from e


class MeteringReconcileResult(BaseModel):
    """Outcome of one metering reconcile sweep."""

    scanned: int
    # Licences that came out of this sweep VERIFIED — i.e. billing now holds
    # everything the plan owes. ⚠️ This is a CONVERGED count, not a fixed count:
    # a licence billing already covered needed no write at all and still lands
    # here. On the first prod sweep expect repaired ≈ 98 with
    # budgets_created ≈ 96 and already_covered ≈ 2. Read `budgets_created` for
    # work actually performed.
    repaired: int
    failed: int
    repaired_license_ids: list[str]
    failed_license_ids: list[str]
    # Licences that OWE billing a token budget and for which billing reported no
    # active ``source='license_sync'`` row AT THE MOMENT IT WAS ASKED.
    #
    # ⚠️ This is the measurement, taken BEFORE any repair — so its meaning
    # depends on the mode:
    #   dry_run=true  → these are the licences that REMAIN uncovered. This is
    #                   the coverage answer, and nothing was written.
    #   repairing run → these are the licences the sweep then TRIED to create a
    #                   budget for. Cross-reference `budgets_created` (how many
    #                   succeeded) and `failed_license_ids`. It is NOT a list of
    #                   what is still broken; re-run with dry_run=true for that.
    uncovered_budget_license_ids: list[str] = []
    # Licences whose coverage billing would not disclose (list call failed). NOT
    # the same as uncovered: nothing was created for these, precisely because
    # creating on an unknown state is how a duplicate row happens.
    unknown_budget_license_ids: list[str] = []
    # Budgets this sweep actually created in billing. The honest "work done"
    # number, and the one to compare against uncovered_budget_license_ids.
    budgets_created: int = 0
    # Licences whose ACTIVE license_sync budget in billing DIFFERS from the
    # licence's effective budget (override, else plan) at the moment it was read.
    # On a repairing run each was re-pushed (billing upserts in place, keeping
    # consumed_tokens); on a dry run nothing was written. Since 2026-10-15.
    budget_drift_license_ids: list[str] = []
    # Budgets this sweep UPDATED in billing to the effective value.
    budgets_updated: int = 0
    # Licences that owed a budget and billing already held one — no write, no
    # repair. Separated from `repaired` so a sweep cannot report converging an
    # estate it merely inspected.
    already_covered: int = 0
    # Licences that owe a budget but live under a BILLING_SENTINEL_TENANT_IDS
    # tenant: billing refuses budgets there by design, so nothing is read or
    # pushed and the budget leg counts as converged (#234). Not a failure.
    budgets_skipped_sentinel: int = 0
    # Licences that owe a budget but whose tenant has ``budget_currency`` set
    # (migration 043): the tenant's budget is MONEY in billing, so no token budget
    # is read or pushed and the budget leg counts as converged. Not a failure.
    budgets_skipped_money_tenant: int = 0
    # Quota configs this sweep CREATED in billing (billing held no row). Before
    # 2026-08-24 this counted every licence with a quota-owing plan, because the
    # push was unconditional — it was a count of writes, not of repairs.
    quotas_pushed: int = 0
    # Licences that owe a quota and for which billing reported NO durable
    # request_quota_config row at the moment it was asked. Same before/after
    # reading rule as uncovered_budget_license_ids: on a dry run this is the
    # coverage answer, on a repairing run it is what was attempted.
    uncovered_quota_license_ids: list[str] = []
    # Licences where billing's row DIFFERS from the plan default and was LEFT
    # ALONE. This is the operator-override signal: a value here is a divergence
    # someone should either bless (change the plan) or drop
    # (force_plan_quota=true). It is NOT a failure and does not block the stamp.
    quota_drift_license_ids: list[str] = []
    # Licences whose configured quota billing would not disclose (read failed or
    # was unparseable). Nothing was pushed for these — an unknown is not an
    # absent. Fail-closed, so they land in `failed` too.
    unknown_quota_license_ids: list[str] = []
    # WHY those quotas were unreadable, counted by cause. Added 2026-09-04 after
    # the 03:41Z CronJob failed both attempts (exit=2, three alerts) reporting
    # only `unknown_quotas=1`; the real cause — billing 404, i.e. its
    # foreign-owner rule at `routers/quota.py:132`, i.e. the licence's tenant of
    # record has diverged between the two databases — took ~30 minutes of Loki
    # archaeology to recover. Keys are `QUOTA_UNKNOWN_REASONS` with
    # `http_<status>` interpolated; `foreign_tenant` is the one with a known
    # remediation (reconcile the tenant_id), `transport`/`http_5xx` just mean
    # re-run. This RIDES ALONGSIDE `unknown_quota_license_ids`, which is
    # unchanged and remains the contract; it is a label, and nothing branches
    # on it — the sweep still fails closed on every reason alike.
    unknown_quota_reasons: dict[str, int] = {}
    # Licences whose billing row already equals the plan default — no write.
    quotas_already_configured: int = 0
    # Quota rows this sweep OVERWROTE because force_plan_quota was set. Non-zero
    # only on an explicit operator opt-in; it is the count of overrides
    # destroyed, and it is separated from quotas_pushed for exactly that reason.
    quotas_overwritten: int = 0
    dry_run: bool = False


async def run_metering_reconcile(
    *,
    limit: int = 500,
    revalidate: bool = False,
    dry_run: bool = False,
    force_plan_quota: bool = False,
    actor: str,
    license_ids: list[str] | None = None,
) -> MeteringReconcileResult:
    """Measure each live licence's metering coverage AGAINST BILLING, and repair it.

    ⚠️ THIS IS THE RUNNER, and it carries NO HTTP and NO AUTH. It exists as a
    plain function because the HTTP route is unreachable to the one identity
    that would schedule it: the route is `require_any_role("license-admin")`,
    `require_any_role` tests REALM roles only, and the transitive composite
    closure of `service-account-licenses` in realm `secure-apps` is
    `billing-internal, default-roles-secure-apps, manage-account,
    manage-account-links, offline_access, service-internal, uma_authorization,
    view-profile` — no `license-admin`. A curl-based CronJob against the route
    would 403 on day one, and granting the SA `license-admin` would hand it
    cross-tenant licence mutation plus billing's pricing routes for zero
    functional gain. `scripts/reconcile_metering.py` calls THIS function
    in-process instead, using the credentials the pod already has.

    `actor` is recorded in the sweep's log line and is the only thing the HTTP
    route contributes beyond argument marshalling — everything below is
    identical whether the caller is an operator or the CronJob.

    Selection = a licence that is LIVE (`status='active'` and not past
    `expires_at` — a past-expiry "active" row is the renewal-deadlock shape, not
    a live entitlement), whose EFFECTIVE metering (the licence's override, else its
    plan's value) OWES billing a quota row OR a token budget, with
    `metering_verified_at IS NULL` (or everything live, with
    `revalidate=true`).

    `license_ids` narrows the sweep to exactly those licence ids and nothing
    else, on top of every other selection rule (so a licence that is not live,
    or whose plan owes billing nothing, is still not selected). It is what makes
    a single-licence canary possible: `limit=1` cannot do that job, because it
    picks whichever of the licences sharing the earliest `created_at` happens to
    sort first. `None` means no id filter; an EMPTY list means select nothing,
    and is not silently treated as "no filter".

    ⚠️ It keys on `metering_verified_at`, NOT `metering_provisioned_at`, and that
    is the whole point. Migration 018 baselined `metering_provisioned_at` across
    the entire table inside the same guard that created it: measured in prod on
    2026-08-10, ALL 208 of 208 rows are non-NULL and 205 of them carry one
    migration timestamp. Keyed on that column this sweep scanned 0 and repaired
    0 while 94 of 98 live budget-owing licences had no active budget in billing
    — it reported the estate clean forever. `metering_verified_at` (migration
    019) is never baselined, so NULL means one thing only: billing has not been
    asked. Also measured 2026-08-10: `metering_verified_at` is NULL on 208 of
    208 prod rows, i.e. this sweep has never run against production.

    BUDGETS ARE REPAIRED READ-BEFORE-WRITE. `billing.token_budgets` has PRIMARY
    KEY (id) and no unique constraint on `license_id`, so this sweep asks
    `GET /api/budget/list/{tenant}` first and creates only what is genuinely
    absent. ⚠️ CHANGED 2026-10-15: a budget billing holds whose `budget_tokens`
    DIFFERS from the licence's effective budget is now re-pushed (billing's
    license_sync upsert updates it in place) and reported in
    `budget_drift_license_ids` / `budgets_updated`; an equal one is left alone.
    The "can silently reset a cap hand-set in billing" hazard described below is
    therefore now the intended behaviour: the licence is the cap's source of
    truth, and a per-licence cap belongs in `token_budget_override`. Coverage counts only `active` rows with `source='license_sync'`:
    `active=False` is billing's soft-delete, and `manual`/`topup` rows stack by
    design (prod carries three tenant-wide `manual` rows under the sentinel
    `license_id='00000000-0000-0000-0000-000000000000'`, one of them 40M tokens
    over a tenant whose 21 licences each still owe their own budget). If billing
    will not answer, the licence is reported in `unknown_budget_license_ids` and
    nothing is created for it — creating on an unknown state is exactly how a
    duplicate row happens.

    QUOTA IS REPAIRED READ-BEFORE-WRITE TOO, since 2026-08-24. It was not, and
    that cost the estate a silent nightly revert: the sweep pushed
    `plan.quota_monthly` unconditionally, so every quota an operator tuned by
    hand in billing was overwritten by the `41 3 * * *` CronJob. Measured in the
    prod billing database on 2026-08-24: 181 `request_quota_config` rows, 155 of
    them rewritten within the previous 25 hours (newest `updated_at`
    2026-08-24 03:41:56), against 155 active licences. The old justification —
    "PRIMARY KEY (license_id), so a re-push leaves at most one row" — was true
    and irrelevant; it bounds the row COUNT while the harm is to the VALUE.

    The read is `GET /api/quota/check` per licence (billing offers no quota list
    form; see `_fetch_quota_config_monthly` for why the plan value must NOT be
    sent as its fallback param). Three outcomes:

    * no row      → push, and count it in `quotas_pushed`.
    * row differs → LEAVE IT, log both values, report the licence in
      `quota_drift_license_ids`. Divergence is made visible, not resolved: this
      sweep cannot tell an operator override from a stale plan edit, and only one
      of those two should win. The licence still converges and is still stamped —
      a deliberate override is not a failure.
    * unreadable  → push nothing, report in `unknown_quota_license_ids`, and fail
      the licence closed. An unknown is not an absent.

    ⚠️ AN UNKNOWN ALSO CARRIES A CAUSE, since 2026-09-04 —
    `unknown_quota_reasons`, a count per reason from `QUOTA_UNKNOWN_REASONS`.
    It changes NOTHING about the behaviour above (every reason fails closed
    identically); it exists because the 2026-09-04 03:41Z CronJob failed both
    attempts with `exit=2` and three firing alerts, and its whole operator-facing
    output was `scanned=157 repaired(converged)=156 failed=1 unknown_quotas=1`.
    Three unrelated causes reach that counter — a non-retryable status, a
    transport failure, an unreadable payload — and only one of them, HTTP 404,
    is a named recurring shape: billing's quota_check 404s ONLY on its
    foreign-owner rule (`routers/quota.py:132`), so a 404 proves a
    `request_quota_config` row exists under a DIFFERENT tenant, i.e. the
    licence's tenant of record has diverged between the licences and billing
    databases. Recovering that from `unknown_quotas=1` took ~30 minutes of Loki
    archaeology; `unknown_quota_reasons={'foreign_tenant': 1}` says it in the
    summary line. `unknown_quota_license_ids` is UNCHANGED.

    `force_plan_quota=true` is the explicit opt-in for the other direction: push
    the plan value over an existing differing row. DEFAULT OFF, counted
    separately in `quotas_overwritten`, and it is a destructive flag — every
    increment is one operator override destroyed. It has no effect on a licence
    billing holds no row for (that is a plain create) or on an unknown.

    ⚠️ KNOWN LIMITATION — coverage is keyed on (tenant, licence). That is forced
    on us: `GET /api/budget/list/{tenant}` is the only budget read billing
    offers. Two live licences in prod carry a STALE LEGACY tenant on their
    billing row (`a005bf37-…` is `portal` in billing but `019de842-c92b-7564-…`
    in licences; `9d96dc27-…` is `n8n-test` vs `019de842-c92b-752e-…`), so their
    existing budgets are INVISIBLE to a read keyed on the licences-side tenant
    and this sweep classes them as uncovered and pushes.

    ⚠️ CORRECTED 2026-08-11 — what that push does was described wrongly twice,
    and each description was milder than the truth.

    It does NOT create a second row. Billing's upsert does not key on tenant:
    `budget_crud._select_active_license_sync_budget` selects on
    `(license_id, period, source='license_sync', active)`, and alembic `0010`
    declares a matching partial UNIQUE INDEX
    `uq_token_budgets_license_sync_active` on `(license_id, period)` WHERE
    `source='license_sync' AND active`. Neither mentions `tenant_id`. Billing
    finds the stale-tenant row every time.

    It MUTATES that row. `budget_crud._apply()` re-tenants it in place
    (`row.tenant_id = tenant_id; row.tenant_name = tenant_id`, following the
    licences-side tenant we sent) and then sets `row.budget_tokens` to the value
    this sweep pushed — the EFFECTIVE budget (the licence's
    `token_budget_override`, else `plan.token_budget_monthly`). So
    RE-RUNNING THIS SWEEP CAN SILENTLY RESET A CAP HAND-SET IN BILLING back to the
    effective value, on any licence whose active `license_sync` budget was tuned by
    hand. `consumed_tokens` is preserved, so usage reporting shows nothing
    unusual and the reset is invisible unless someone diffs the cap.

    Billing's half of this is deliberate and correct on its own terms — the
    licence-keyed key and the in-place re-tenanting are exactly alembic 0010's
    intent ("The licence moved tenants upstream. One licence, one tenant."). No
    billing change is implied and none should be made from this repo. Note also
    that the tenant repair happens as a SIDE EFFECT of a sweep run; that half is
    a fix, the `budget_tokens` overwrite is the hazard.

    History, so neither wrong version gets re-derived: (1) the original text
    claimed the second row would "stack the entitlement" (10M + 10M = 20M
    enforced) — refuted 2026-08-10, and it was the standing reason not to run
    the sweep at all; (2) the 2026-08-10 replacement claimed a harmless
    "permanently UNREACHABLE ORPHAN row" — refuted here, since no second row is
    created at all. The stacking refutation still holds on its own facts and is
    worth keeping: `_select_active_budgets`
    (`billing_service/src/services/budget_debit.py`) filters
    `TokenBudget.tenant_id == tenant_id` before anything else, and the Redis
    gate `_budget_redis_key` (`.../budget_redis.py`) is
    `budget:limit:{license_id}:{period}` — licence-scoped, tenant absent — so
    nothing anywhere sums two rows.

    ⚠️ The `0010` index is read from billing's repo; it was NOT re-verified
    against the live billing database on 2026-08-11 (that read was unavailable).
    The overwrite hazard does not depend on the index: the licence-keyed SELECT
    alone produces it. Repair the two stale `tenant_id`s in
    `billing.token_budgets` first, or run `dry_run=true` and check the report
    against the table before letting a sweep write.

    Idempotent for the licences side: a licence is stamped only after billing
    accepts, and BOTH pushes now happen only when billing's own read said it
    holds nothing — the budget push on the tenant-scoped list, the quota push on
    the per-licence quota check — so a repeat run makes no NEW push, with or
    without `revalidate`. That is not the same as "a repeat run is a no-op in
    billing": for a stale-tenant licence the BUDGET leg pushes every time,
    because the tenant-keyed read can never see the row it is about to overwrite.
    The quota leg has no such hole — its read is licence-keyed — but the same
    stale tenant makes billing 404 the read, which this treats as UNKNOWN and
    therefore pushes NOTHING.

    ⚠️ READING THE RESULT. `repaired` is a CONVERGED count, not a fixed count —
    a licence billing already covered needed no write and still lands in it. The
    honest work-done numbers are `budgets_created` and `already_covered`, and
    `uncovered_budget_license_ids` is the measurement taken BEFORE any repair
    (so on a repairing run it lists what was attempted, not what remains). To
    ask "what is still broken", run again with `dry_run=true`.
    """
    import httpx

    wanted_ids: list[UUID] | None = None
    if license_ids is not None:
        try:
            wanted_ids = [UUID(str(one)) for one in license_ids]
        except ValueError as e:
            # 400, not 500: a malformed id is the caller's, and COMMON_ERROR_RESPONSES
            # already documents 400 as "invalid UUID". Raised BEFORE the scan so a
            # typo'd canary id can never be read as "that licence is already fine".
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="license_id must be a UUID",
            ) from e

    def _scan() -> list[Any]:
        """The gap scan, as a unit of work for a worker thread.

        Extracted into a plain ``def`` rather than left inline so the whole
        session lifetime — connect, SELECT, close — happens on ONE thread. Under
        ``NullPool`` this is a fresh physical connection, and materialising up to
        ``limit`` rows (ceiling 5000) on the event loop stalled every other
        request on the pod before the coroutine reached its first ``await``.
        """
        session = db_manager.get_session()
        try:
            now = datetime.now(UTC)
            query = (
                session.query(
                    License.id,
                    License.tenant_id,
                    # Plan.id is selected ONLY because billing's quota read
                    # (`GET /api/quota/check`) requires plan_id as a mandatory
                    # query param. It is not a selection predicate and must never
                    # become one.
                    Plan.id,
                    Plan.quota_monthly,
                    Plan.token_budget_monthly,
                    # #239 / migration 041: the licence's own overrides ride along
                    # so the sweep pushes and compares the EFFECTIVE values
                    # (``effective_metering_values``), never the bare plan's. A
                    # repair that re-pushed the plan value replaced an operator's
                    # per-licence budget with the plan default — raising the
                    # tenant's POOLED spend cap — and reported the override as drift.
                    License.token_budget_override,
                    License.quota_override,
                    # Migration 043: a money tenant owes no token budget, so the
                    # sweep must not (re)create one. OUTER join: a licence whose
                    # tenant row is missing reads NULL = unchanged behaviour.
                    Tenant.budget_currency,
                    # The app rides along so the quota push can carry it. Billing's
                    # resolver ignores any quota-config row whose ``app_id`` is
                    # NULL, so a reconcile that repairs a MISSING row without the
                    # app creates a row that can never resolve — the gap it was
                    # meant to close, reopened one field lower down.
                    # INNER join: ``License.app_id`` is ``nullable=False``, so this
                    # cannot drop a row that the old query would have returned.
                    App.id,
                    App.name,
                )
                .join(Plan, Plan.id == License.plan_id)
                .join(App, App.id == License.app_id)
                .outerjoin(Tenant, Tenant.id == License.tenant_id)
                .filter(License.status == "active")
                .filter((License.expires_at.is_(None)) | (License.expires_at > now))
                .filter(
                    # "Owes billing something" on the EFFECTIVE values: an override
                    # on an unlimited plan makes the licence owe that leg. Mirrored
                    # by the hand-written count in scripts/reconcile_metering.py.
                    (Plan.quota_monthly.isnot(None))
                    | (Plan.token_budget_monthly.isnot(None))
                    | (License.quota_override.isnot(None))
                    | (License.token_budget_override.isnot(None)),
                )
            )
            if not revalidate:
                query = query.filter(License.metering_verified_at.is_(None))
            if wanted_ids is not None:
                # NARROWS, never widens: applied on top of the live/owing/stamp
                # filters above, so a named licence that is revoked or on a plan
                # owing billing nothing is still not selected.
                query = query.filter(License.id.in_(wanted_ids))
            return query.order_by(License.created_at).limit(limit).all()
        finally:
            session.close()

    try:
        gaps = await asyncio.to_thread(_scan)
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Metering reconcile scan failed",
            e,
            "Metering reconcile scan failed",
        ) from e

    repaired: list[str] = []
    failed: list[str] = []
    uncovered: list[str] = []
    unknown: list[str] = []
    uncovered_quota: list[str] = []
    quota_drift: list[str] = []
    unknown_quota: list[str] = []
    # WHY the unknowns are also counted BY CAUSE, added 2026-09-04: see
    # `QUOTA_UNKNOWN_REASONS`. `unknown_quota_license_ids` stays exactly as it
    # was — it is the documented contract and something may consume it — and
    # this rides alongside it so `unknown_quotas=1` stops being a number an
    # operator has to go to Loki to interpret.
    unknown_quota_reasons: dict[str, int] = {}
    budget_drift: list[str] = []
    budgets_created = 0
    budgets_updated = 0
    already_covered = 0
    budgets_skipped_sentinel = 0
    budgets_skipped_money_tenant = 0
    quotas_pushed = 0
    quotas_already_configured = 0
    quotas_overwritten = 0

    if gaps:
        logger.warning(
            "Metering reconcile: examining %d live licence(s) whose billing coverage has "
            "not been verified (requested by %s, dry_run=%s, revalidate=%s, "
            "force_plan_quota=%s)",
            len(gaps),
            actor,
            dry_run,
            revalidate,
            force_plan_quota,
        )
        if force_plan_quota:
            logger.warning(
                "Metering reconcile: force_plan_quota=true — an existing billing quota that "
                "differs from the licence's effective value (its stored override, else the "
                "plan default) WILL be overwritten with that value. Any operator-set cap in "
                "billing that is not recorded on the licence is destroyed by this run.",
            )
        # One budget-list read per TENANT, not per licence: the estate's gap is
        # concentrated in a handful of tenants (21 + 18 + 17 + 12 of 94 in prod).
        # ``None`` is cached too — a tenant billing would not answer for must not
        # be retried 21 times in one sweep.
        budget_ids_by_tenant: dict[str, dict[str, int | None] | None] = {}

        async with httpx.AsyncClient(timeout=5.0) as client:
            for (
                license_id,
                tenant_id,
                plan_id,
                plan_quota_monthly,
                plan_token_budget_monthly,
                token_budget_override,
                quota_override,
                budget_currency,
                app_id,
                app_name,
            ) in gaps:
                # From here on `token_budget_monthly` / `quota_monthly` are the
                # licence's EFFECTIVE values (override, else plan) — the only
                # values this sweep may push or compare against (#239).
                token_budget_monthly, quota_monthly = effective_metering_values(
                    token_budget_override,
                    quota_override,
                    plan_token_budget_monthly,
                    plan_quota_monthly,
                )
                lic = str(license_id)
                tenant = str(tenant_id)
                headers = await _metering_auth_headers(tenant, lic)

                budget_ok = True
                if token_budget_monthly is not None and budget_currency is not None:
                    # Migration 043 / PLAN §2.2: a MONEY tenant. Billing holds its
                    # budget as money; a license_sync token budget would be a second
                    # pooled cap, and re-creating it nightly is exactly what this
                    # skip exists to stop. No read, no push; the leg is converged.
                    budgets_skipped_money_tenant += 1
                    logger.info(
                        "token_budget_skipped_money_tenant license=%s tenant=%s "
                        "budget_currency=%s — reconcile pushes no token budget",
                        lic,
                        tenant,
                        budget_currency,
                    )
                elif token_budget_monthly is not None and tenant in BILLING_SENTINEL_TENANT_IDS:
                    # #234: billing refuses budgets for its sentinel tenant BY
                    # DESIGN. No read, no push; the budget leg is converged.
                    # Quota handling below is unchanged — billing accepts those.
                    budgets_skipped_sentinel += 1
                    logger.info(
                        "Metering reconcile: skipping token budget for license=%s tenant=%s "
                        "— sentinel tenant, billing refuses budgets by design (#234)",
                        lic,
                        tenant,
                    )
                elif token_budget_monthly is not None:
                    if tenant not in budget_ids_by_tenant:
                        budget_ids_by_tenant[tenant] = await _fetch_license_sync_budget_ids(
                            client, tenant_id=tenant, headers=headers
                        )
                    covered_ids = budget_ids_by_tenant[tenant]
                    effective_budget = int(token_budget_monthly)
                    if covered_ids is None:
                        unknown.append(lic)
                        budget_ok = False
                    elif lic in covered_ids and covered_ids[lic] not in (None, effective_budget):
                        # DIFFERS (2026-10-15). Licences is the source of truth for
                        # a licence's cap (override, else plan); a value billing
                        # holds that disagrees is a cap change that never reached
                        # billing — e.g. a PATCH /plans before update-time pushes
                        # existed, or a push that failed. Billing's license_sync
                        # POST is an upsert: it updates budget_tokens, keeps
                        # consumed_tokens, and rewrites the Redis limit the
                        # ingress reads. A cap set by hand in billing that is not
                        # recorded on the licence (token_budget_override) IS
                        # overwritten here — record it on the licence instead.
                        budget_drift.append(lic)
                        logger.warning(
                            "Metering reconcile: billing budget for license=%s tenant=%s is "
                            "%s, the licence's effective budget is %s — %s",
                            lic,
                            tenant,
                            covered_ids[lic],
                            effective_budget,
                            "dry run, nothing pushed" if dry_run else "pushing the upsert",
                        )
                        if not dry_run:
                            budget_ok = await _push_token_budget(
                                client,
                                license_id=lic,
                                tenant_id=tenant,
                                budget_tokens=effective_budget,
                                headers=headers,
                            )
                            if budget_ok:
                                budgets_updated += 1
                                covered_ids[lic] = effective_budget
                    elif lic in covered_ids:
                        # billing already holds it at the effective value (or at a
                        # value it would not disclose). No write.
                        # Counted separately: this licence is converged, but the
                        # sweep did no work on it and must not claim otherwise.
                        already_covered += 1
                    else:
                        uncovered.append(lic)
                        if not dry_run:
                            budget_ok = await _push_token_budget(
                                client,
                                license_id=lic,
                                tenant_id=tenant,
                                budget_tokens=effective_budget,
                                headers=headers,
                            )
                            if budget_ok:
                                budgets_created += 1
                                # Keep the cache truthful within this sweep so a
                                # second licence cannot re-read a stale "absent".
                                covered_ids[lic] = effective_budget

                quota_ok = True
                if quota_monthly is not None:
                    # READ BEFORE WRITE. The push overwrites `quota_monthly` on
                    # billing's existing row, so an unconditional push reverts
                    # every operator-set cap in the estate on every run — which
                    # is precisely what it did nightly until 2026-08-24. The
                    # read happens on a dry run too: measuring is the one thing
                    # a dry run is FOR.
                    # EFFECTIVE quota (licence override, else plan) — see above.
                    plan_quota = int(quota_monthly)
                    (
                        quota_state,
                        current_quota,
                        quota_unknown_reason,
                    ) = await _fetch_quota_config_monthly(
                        client,
                        license_id=lic,
                        plan_id=str(plan_id),
                        headers=headers,
                    )
                    if quota_state == "unknown":
                        # Fail CLOSED, same rule as the budget leg: nothing is
                        # written on a state we could not see. The reason is
                        # RECORDED, never branched on — it is a label for the
                        # operator, and the moment it decides anything the
                        # fail-closed guard has a hole in it.
                        unknown_quota.append(lic)
                        reason = quota_unknown_reason or "unspecified"
                        unknown_quota_reasons[reason] = unknown_quota_reasons.get(reason, 0) + 1
                        quota_ok = False
                    elif quota_state == "absent":
                        uncovered_quota.append(lic)
                        if not dry_run:
                            quota_ok = await _push_quota_config(
                                client,
                                license_id=lic,
                                tenant_id=tenant,
                                quota_monthly=plan_quota,
                                headers=headers,
                                app_id=str(app_id),
                                app_name=app_name,
                            )
                            if quota_ok:
                                quotas_pushed += 1
                    elif current_quota == plan_quota:
                        quotas_already_configured += 1
                    elif force_plan_quota:
                        quota_drift.append(lic)
                        logger.warning(
                            "Metering reconcile OVERWRITING an operator quota for license=%s "
                            "tenant=%s: billing held quota_monthly=%s, forcing the plan "
                            "default %s because force_plan_quota=true. This is destructive "
                            "and was explicitly asked for.",
                            lic,
                            tenant,
                            current_quota,
                            plan_quota,
                        )
                        if not dry_run:
                            quota_ok = await _push_quota_config(
                                client,
                                license_id=lic,
                                tenant_id=tenant,
                                quota_monthly=plan_quota,
                                headers=headers,
                                app_id=str(app_id),
                                app_name=app_name,
                            )
                            if quota_ok:
                                quotas_overwritten += 1
                    else:
                        # DIVERGENCE, MADE VISIBLE — not resolved. This sweep
                        # cannot distinguish an operator override from a stale
                        # plan edit, and silently picking either one is how the
                        # nightly revert happened. One line per licence, both
                        # values, and the licence still converges: a deliberate
                        # override is not a failure.
                        quota_drift.append(lic)
                        logger.warning(
                            "Metering reconcile LEFT a divergent quota alone for license=%s "
                            "tenant=%s: billing holds quota_monthly=%s, the licence's effective "
                            "quota (its override, else the plan default) is %s. Nothing was "
                            "pushed. Either bless billing's value by setting "
                            "license.quota_override (or changing the plan), or re-run with "
                            "force_plan_quota=true to push the effective value over it.",
                            lic,
                            tenant,
                            current_quota,
                            plan_quota,
                        )

                if dry_run:
                    continue
                # to_thread: _mark_metering_verified is blocking SQL (connect ->
                # UPDATE -> COMMIT -> close, a FRESH physical connection under
                # NullPool) and this is the per-licence loop — up to `limit` of
                # them, ceiling 5000, serially. On the loop thread that froze
                # every other request on the pod for the whole sweep.
                #
                # ⚠️ THE GOVERNOR ARGUMENT APPLIES TO THE HTTP ROUTE ONLY, and an
                # earlier version of this comment wrongly claimed it for both
                # callers. In the SERVED pod these connections now land in the
                # `licenses-to-thread` executor and are inside the 16-per-pod
                # budget — measured, by thread name. In the CronJob
                # (`scripts/reconcile_metering.py`) they are NOT: that process
                # calls `asyncio.run` with no lifespan, so `set_default_executor`
                # never runs and `to_thread` uses the stock executor
                # (`asyncio_N`). Harmless — the sweep's `to_thread` calls are
                # strictly serial, so its concurrency is one connection either
                # way, and it is a separate process from the serving pods — but
                # it is not the governor doing that work, and saying so would be
                # a false reassurance about the pgbouncer budget.
                if budget_ok and quota_ok and await asyncio.to_thread(_mark_metering_verified, lic):
                    repaired.append(lic)
                else:
                    failed.append(lic)
                    logger.error(
                        "%s license=%s tenant=%s budget_ok=%s quota_ok=%s — reconcile could "
                        "not converge this licence; it remains (partly) unmetered",
                        METERING_FAILURE_MARKER,
                        lic,
                        tenant,
                        budget_ok,
                        quota_ok,
                    )

    logger.info(
        "Metering reconcile complete: scanned=%d repaired(converged)=%d failed=%d "
        "uncovered_budgets=%d unknown_budgets=%d budgets_created=%d budget_drift=%d "
        "budgets_updated=%d already_covered=%d "
        "budgets_skipped_sentinel=%d budgets_skipped_money_tenant=%d "
        "uncovered_quotas=%d unknown_quotas=%d unknown_quota_reasons=%s quota_drift=%d "
        "quotas_pushed=%d "
        "quotas_already_configured=%d quotas_overwritten=%d dry_run=%s",
        len(gaps),
        len(repaired),
        len(failed),
        len(uncovered),
        len(unknown),
        budgets_created,
        len(budget_drift),
        budgets_updated,
        already_covered,
        budgets_skipped_sentinel,
        budgets_skipped_money_tenant,
        len(uncovered_quota),
        len(unknown_quota),
        # `unknown_quotas=1` on its own sent an operator to Loki for half an
        # hour on 2026-09-04; the breakdown names the cause in the same line.
        dict(sorted(unknown_quota_reasons.items())),
        len(quota_drift),
        quotas_pushed,
        quotas_already_configured,
        quotas_overwritten,
        dry_run,
    )
    return MeteringReconcileResult(
        scanned=len(gaps),
        repaired=len(repaired),
        failed=len(failed),
        repaired_license_ids=repaired,
        failed_license_ids=failed,
        uncovered_budget_license_ids=uncovered,
        unknown_budget_license_ids=unknown,
        budgets_created=budgets_created,
        budget_drift_license_ids=budget_drift,
        budgets_updated=budgets_updated,
        already_covered=already_covered,
        budgets_skipped_sentinel=budgets_skipped_sentinel,
        budgets_skipped_money_tenant=budgets_skipped_money_tenant,
        uncovered_quota_license_ids=uncovered_quota,
        quota_drift_license_ids=quota_drift,
        unknown_quota_license_ids=unknown_quota,
        unknown_quota_reasons=dict(sorted(unknown_quota_reasons.items())),
        quotas_pushed=quotas_pushed,
        quotas_already_configured=quotas_already_configured,
        quotas_overwritten=quotas_overwritten,
        dry_run=dry_run,
    )


@app.post(
    METERING_RECONCILE_PATH,
    response_model=MeteringReconcileResult,
    operation_id="reconcile_metering_provisioning",
    tags=["licenses"],
    responses=COMMON_ERROR_RESPONSES,
)
async def reconcile_metering_provisioning(
    limit: int = Query(500, ge=1, le=5000, description="Max licences repaired in one sweep"),
    revalidate: bool = Query(
        False,
        description=(
            "Re-examine licences already stamped metering_verified_at instead of "
            "skipping them. Use to re-measure coverage on demand."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Measure coverage against billing and report it, writing nothing — "
            "no pushes to billing, no stamps."
        ),
    ),
    force_plan_quota: bool = Query(
        False,
        description=(
            "DESTRUCTIVE, default off. Push the licence's EFFECTIVE quota (its "
            "quota_override, else plan.quota_monthly) over a billing quota row that "
            "DIFFERS from it, destroying the hand-set billing value "
            "that made it differ. Without this, a divergent row is left alone "
            "and reported in quota_drift_license_ids. Has no effect where "
            "billing holds no row (that is a plain create) or where the "
            "configured quota could not be read."
        ),
    ),
    license_id: str | None = Query(
        None,
        description=(
            "Restrict the sweep to this ONE licence id (a canary). Narrows the "
            "normal selection, never widens it: a licence that is not live, or "
            "whose EFFECTIVE metering (override or plan) owes billing nothing, is "
            "still not swept. 400 if not a "
            "UUID. Omit to sweep the estate."
        ),
    ),
    user: User = Depends(require_any_role("license-admin")),
) -> MeteringReconcileResult:
    """Measure each live licence's metering coverage AGAINST BILLING, and repair it.

    Requires `license-admin`. Deliberately cross-tenant, same as
    `GET /licenses/admin/all` — the caller is the platform-admin surface acting
    for a cross-tenant operator, and the gap this repairs is estate-wide.

    A THIN WRAPPER in code: it contributes exactly two things, the
    `license-admin` check and the actor name for the audit log line. All
    behaviour lives in `run_metering_reconcile`, so this route and the scheduled
    `scripts/reconcile_metering.py` cannot drift apart. The scheduled path exists
    because the licences service account does NOT hold `license-admin` and never
    should — see the runner's docstring for the role closure.

    Selection = a licence that is LIVE (`status='active'` and not past
    `expires_at` — a past-expiry "active" row is the renewal-deadlock shape, not
    a live entitlement), whose EFFECTIVE metering (the licence's override, else its
    plan's value) OWES billing a quota row OR a token budget, with
    `metering_verified_at IS NULL` (or everything live, with
    `revalidate=true`). `license_id` narrows that further to one licence — it
    NEVER widens it, and it is the only way to canary a single licence (`limit=1`
    picks whichever licence sharing the earliest `created_at` sorts first).

    BUDGETS ARE REPAIRED READ-BEFORE-WRITE. `billing.token_budgets` has PRIMARY
    KEY (id) and no unique constraint on `license_id`, so this sweep asks
    `GET /api/budget/list/{tenant}` first and creates only what is genuinely
    absent. ⚠️ CHANGED 2026-10-15: a budget billing holds whose `budget_tokens`
    DIFFERS from the licence's effective budget is now re-pushed (billing's
    license_sync upsert updates it in place) and reported in
    `budget_drift_license_ids` / `budgets_updated`; an equal one is left alone.
    The "can silently reset a cap hand-set in billing" hazard described below is
    therefore now the intended behaviour: the licence is the cap's source of
    truth, and a per-licence cap belongs in `token_budget_override`. If billing will not answer, the licence is reported in
    `unknown_budget_license_ids` and nothing is created for it — creating on an
    unknown state is exactly how a duplicate row happens.

    Coverage counts ONLY `active` rows with `source='license_sync'`, and both
    halves of that are load-bearing. `active=False` is billing's soft-delete and
    enforces nothing (of 185 `license_sync` rows in prod, 176 are soft-deleted —
    counting them would call 185 licences covered). `manual`/`topup` rows stack
    BY DESIGN and are frequently tenant-wide under the sentinel
    `license_id='00000000-0000-0000-0000-000000000000'`; prod holds three, one of
    them 40M tokens over a tenant whose 21 licences each still owe their own
    licence-keyed budget, and counting it would call all 21 covered.

    QUOTA is re-pushed rather than read first: `billing.request_quota_config` has
    PRIMARY KEY (license_id), so a re-push leaves at most one row — and there is
    no licence-keyed quota read in billing's API to check against anyway.

    ⚠️ KNOWN LIMITATION — coverage is keyed on (tenant, licence), because a
    tenant-scoped list is the only budget read billing offers. Billing's own
    upsert does NOT key on the same tuple, and that asymmetry is the hazard. Two
    live licences in prod carry a STALE LEGACY tenant on their billing row
    (`a005bf37-…` is `portal` in billing but `019de842-c92b-7564-…` in licences;
    `9d96dc27-…` is `n8n-test` vs `019de842-c92b-752e-…`). Their budgets are
    invisible to a read keyed on the licences-side tenant, so the sweep classes
    them as uncovered and pushes.

    That push does NOT create a second row. Billing selects on
    `(license_id, period, source='license_sync', active)` — no `tenant_id` — so
    it finds the stale-tenant row every time. It MUTATES it: re-tenants the row
    in place and then sets `budget_tokens` to the value this sweep pushed, the
    EFFECTIVE value (override or plan). So RE-RUNNING THIS SWEEP CAN SILENTLY RESET AN OPERATOR-SET
    CAP, on any licence whose active `license_sync` budget was tuned by hand.
    `consumed_tokens` is preserved, so usage reporting looks normal and the
    reset is invisible unless someone diffs the cap.

    Correct those two rows' `tenant_id` first, or run `dry_run=true` and check
    the report against `billing.token_budgets`.

    (Billing's half is deliberate and correct on its own terms; no billing change
    is implied. The full derivation, and the two earlier WRONG descriptions kept
    so neither gets re-derived, are in `run_metering_reconcile`'s docstring —
    this route is a thin wrapper over it and the two must not drift apart again.
    They did: #117 corrected the runner and left this copy stating the opposite.)

    QUOTA IS REPAIRED READ-BEFORE-WRITE TOO, since 2026-08-24. Until then this
    sweep pushed `plan.quota_monthly` unconditionally, and since the push
    OVERWRITES billing's row it reverted every hand-set quota in the estate every
    night (prod, 2026-08-24: 155 of 181 `request_quota_config` rows rewritten
    within 25h by the 03:41 CronJob, against 155 active licences). Now: a licence
    with no row is pushed; a row that DIFFERS from the effective value (override or
    plan, #239) is left alone
    and reported in `quota_drift_license_ids`; a quota that cannot be read is
    reported in `unknown_quota_license_ids` and the licence fails closed.
    `force_plan_quota=true` is the explicit, default-off opt-in to push the
    effective value (override or plan) over a divergent row, counted in
    `quotas_overwritten`; the flag keeps its historical name.

    Since 2026-09-04 an unknown also reports its CAUSE in `unknown_quota_reasons`
    (a count keyed by `QUOTA_UNKNOWN_REASONS`). Observability only — the
    fail-closed behaviour is byte-identical for every reason. `foreign_tenant`
    is the one worth acting on: billing's `quota_check` 404s only on its
    foreign-owner rule (`routers/quota.py:132`), so it proves a
    `request_quota_config` row exists under a different tenant and the licence's
    tenant of record has diverged between the two databases — compare
    `licenses.license.tenant_id` against `billing.request_quota_config.tenant_id`
    for that `license_id`. `transport` and `http_5xx` just mean re-run. The
    driver was the 2026-09-04 03:41Z CronJob failing both attempts on
    `unknown_quotas=1`, which named none of this. `unknown_quota_license_ids` is
    unchanged.

    Idempotent: a licence is stamped only after billing accepts, and both the
    budget and quota pushes happen only when billing said it holds nothing — so a
    repeat run creates nothing, with or without `revalidate`.

    ⚠️ READING THE RESULT. `repaired` is a CONVERGED count, not a fixed count —
    a licence billing already covered needed no write and still lands in it. The
    honest work-done numbers are `budgets_created` and `already_covered`, and
    `uncovered_budget_license_ids` is the measurement taken BEFORE any repair
    (so on a repairing run it lists what was attempted, not what remains). To
    ask "what is still broken", run again with `dry_run=true`.

    ⚠️ NEITHER `scanned: 0` NOR AN EMPTY `failed` PROVES A HEALTHY ESTATE.
    After one repairing sweep every licence is stamped, so `revalidate=false`
    selects nothing forever; and a `dry_run` writes nothing, so its `failed` is
    empty by construction no matter how bad `uncovered_budget_license_ids` is.
    Read `scanned` against the size of the estate and `uncovered_…` against zero.
    `scripts/reconcile_metering.py` enforces exactly that as an exit code, which
    is why the scheduled job is the runner and not a curl of this route.
    """
    return await run_metering_reconcile(
        limit=limit,
        revalidate=revalidate,
        dry_run=dry_run,
        force_plan_quota=force_plan_quota,
        actor=user.preferred_username or user.sub,
        license_ids=[license_id] if license_id is not None else None,
    )


@app.post(
    "/licenses/{license_id}/tokens",
    response_model=TokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_license_token",
    tags=["tokens"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "No token was minted. `detail.error` is `license_not_active`: the "
                "license is revoked, expired, soft-deleted or in any stored status other "
                "than `active`. `detail` also carries `status` (effective), "
                "`stored_status`, `starts_at` and `expires_at` — the same shape as "
                "`regenerate-token`'s `license_not_active`. Renew or investigate the "
                "license; retrying cannot succeed. A license that is `pending` only "
                "because `starts_at` is in the future is NOT refused (provisioning "
                "mints on forward-dated grants); its token stays inactive at "
                "introspection until `starts_at` passes."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "Forbidden. Either the caller holds neither `license-admin` nor "
                "`license-manager`, or `detail.error` is `long_lived_requires_license_admin`: "
                "`long_lived=true` mints a PERPETUAL token, and only `license-admin` may "
                "register one. `license-manager` may still mint bounded tokens. Or "
                "`dependency_licence`: the licence exists only because a bundled app "
                "requires this app (migration 059); it is minted through the internal "
                "bundle-token route only. No token was minted."
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "No token was minted: the new-token expiry policy refused it (licenses#244), "
                "or a parameter failed validation. `detail.error` is one of "
                "`perpetual_token_not_registered` (an internal/node mint without "
                "`expires_days` and without `long_lived`), `long_lived_not_permitted` "
                "(long-lived `api` off the n8n licence-token tenants), "
                "`long_lived_requires_owner_and_reason`, `long_lived_with_expiry`, or "
                "`long_lived_fields_without_long_lived`. An `api` mint without "
                "`expires_days` is NOT refused: it gets the bounded default (365 days), "
                "unless the caller's verified issuer + client (`iss`, `azp`) is registered in "
                "LONG_LIVED_IMPLICIT_CLIENTS, which gets a registered long-lived token."
            ),
        },
    },
)
def create_license_token(
    license_id: str,
    token_type: TokenType = Query(TokenType.api, description="Token type: api, node, internal"),
    expires_days: int | None = Query(
        None,
        ge=1,
        description=(
            "Expiration in days. Must be >= 1 — 0 and negatives are rejected (422). "
            "Omitted: an `api` token gets the bounded default "
            "(TOKEN_API_DEFAULT_EXPIRY_DAYS, 365), except from a caller whose verified "
            "issuer + client (`iss`, `azp`) is registered in LONG_LIVED_IMPLICIT_CLIENTS, which gets a "
            "registered long-lived token; an `internal`/`node` token is "
            "REFUSED (422 `perpetual_token_not_registered`) unless `long_lived` is set. "
            "Omitting it no longer means perpetual (licenses#244)."
        ),
    ),
    long_lived: bool = Query(
        False,
        description=(
            "Register a PERPETUAL token in the long-lived register. Requires the "
            "`license-admin` role (license-manager gets 403 "
            "`long_lived_requires_license_admin`), `long_lived_owner` and "
            "`long_lived_reason`, and no `expires_days`. "
            "Allowed for `internal`/`node`, and for `api` only on the n8n licence-token "
            "tenants (LONG_LIVED_API_TENANTS)."
        ),
    ),
    long_lived_owner: str | None = Query(
        None, max_length=200, description="Who owns this long-lived token."
    ),
    long_lived_reason: str | None = Query(
        None, max_length=500, description="Why this token may be perpetual."
    ),
    tenant_id: str = Depends(get_tenant_id),
    user: User = Depends(require_any_role("license-admin", "license-manager")),
):
    """
    Create a new token for a license

    Requires 'license-admin' or 'license-manager' role; `long_lived=true` requires
    'license-admin' (403 `long_lived_requires_license_admin` otherwise)
    Verifies tenant ownership of the license

    ⛔ REFUSES AN ARCHIVED TENANT. This was the last mint-shaped path in this
    service that an archived tenant could still walk. `POST /licenses` already
    refused to create a LICENCE for one, and `/license/introspect` already refuses
    to authorize with one — but nothing stopped a brand-new, long-lived CREDENTIAL
    being issued against a licence the archived tenant already held. See the
    comment on the check below for why that mattered and what it still does not
    reach.
    """
    try:
        # ⛔ A PERPETUAL MINT IS A license-admin DECISION (licenses#244 validator).
        # `introspect` never reads `token_type`, so a long-lived `internal` token is
        # functionally a perpetual api credential: the internal/node allowlist in
        # token_expiry_refusal is a label, not a control. license-manager keeps
        # every bounded mint; only the register itself is gated. Checked before
        # any lookup, so the refusal writes nothing. In-repo scripts that call
        # db_manager.create_token directly (provision_service_token, the n8n
        # seed) never pass this route and are unaffected.
        if long_lived and not user.has_realm_role("license-admin"):
            logger.warning(
                "Refusing long-lived mint for licence %s: actor %s lacks license-admin",
                license_id,
                user.preferred_username,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "long_lived_requires_license_admin",
                    "remedy": (
                        "a perpetual (long_lived) token needs the license-admin role; "
                        "license-manager may mint with expires_days"
                    ),
                },
            )

        license = db_manager.get_license(UUID(license_id))
        if not license:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")
        # Verify tenant owns this license. tenant_id is a UUID column since
        # migration 016; str() it to compare against the string header value.
        if str(license.tenant_id) != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")

        _authorize_issuance(user, license.app_id, tenant_id, "token_create")
        _refuse_dependency_licence(license, "token_create")

        # ⛔ AN ARCHIVED TENANT MAY NOT BE ISSUED A NEW CREDENTIAL.
        #
        # THE GAP THIS CLOSES. Archiving already refuses at the two obvious
        # places: `POST /licenses` will not mint a licence for an archived tenant,
        # and `_introspect_from_session` answers `active: false, reason:
        # "tenant_archived"`. This route was neither. An operator who retired a
        # tenant could still mint a fresh `expires_days=None` (perpetual) token
        # against a licence that tenant already held — a credential born AFTER the
        # retirement, outliving it, and invisible in `list_tenants` because the
        # tenant is filtered out of the picker.
        #
        # ⚠️ WHAT IT IS WORTH, STATED HONESTLY. The token it mints would be
        # refused at introspect anyway while `archived_at` is set, so this is not
        # the difference between spending and not spending TODAY. What it prevents
        # is a credential whose existence outlives the decision to retire the
        # tenant: un-archive later, for any reason, and every token minted in the
        # interval silently becomes live. Refusing at the mint keeps the set of
        # credentials a restored tenant wakes up with equal to the set it had when
        # it was retired.
        #
        # ⛔ REFUSAL, NOT REVOCATION — the same decision `set_tenant_archived`
        # documents. Nothing here revokes an existing token, writes `revoked_at`,
        # or touches a row. Archiving must stay a true round trip; making it
        # destructive would buy an off-switch by removing the restore.
        #
        # ⛔ AND IT IS *NOT* WHAT MAKES `TenantSpendOnArchivedRegistryRow` CLEAR.
        # That alert's population is cost events carrying `license_id = NULL`,
        # posted against a bare `tenant_id` by writers that never call this
        # service at all. No gate in this repo can see that traffic. Do not read a
        # still-firing alert as this check failing.
        #
        # Gate on the WRITE path, not inside `get_tenant` — `get_tenant` has ~10
        # call sites and one of them is the authz hot path, where hiding the row
        # would turn "retired" into "every in-flight request fails". Same
        # reasoning, and the same shape, as the mint guard in `create_user_license`.
        token_tenant = db_manager.get_tenant(str(license.tenant_id))
        if token_tenant is not None and token_tenant.archived_at is not None:
            logger.warning(
                "Refusing to mint token for licence %s: tenant %s (%s) is ARCHIVED "
                "(archived_at=%s). The licence row is still %r; archiving the tenant is "
                "what makes a new credential refusable. Un-archive the tenant "
                "deliberately — do not exempt it here.",
                license_id,
                tenant_id,
                token_tenant.name,
                token_tenant.archived_at,
                license.status,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "refusing to mint: tenant is archived — un-archive the tenant "
                    "before creating a token"
                ),
            )

        # ⛔ A DEAD LICENCE MAY NOT BE ISSUED A NEW CREDENTIAL (#237).
        #
        # This route checked existence, ownership and the archived tenant, then
        # minted — with no licence-state check at all — while its sibling
        # `regenerate_token` has 409'd on a non-active licence since 2026-08-08.
        # A token minted on a revoked/expired licence is refused at introspect
        # TODAY, but it is a credential whose existence outlives the decision to
        # kill the licence: any later write that flips `status` back to 'active'
        # wakes it up. Same reasoning as the archived-tenant gate above.
        #
        # ⚠️ DELIBERATELY NOT `is_active()`. `accepts_new_token()` allows ONE case
        # `is_active()` refuses: `pending` because `starts_at` is in the future.
        # provisioning mints its one-time raw token here right after
        # `create_license` with a caller-supplied `starts_at`
        # (provisioning/src/licensing.py step 5 says it chose THIS route because
        # it has no validity gate; also src/orchestration.py). Refusing pending
        # would break every forward-dated grant. Measured before this change:
        # 0 tokens minted on revoked/expired licences in 30 days, so refusing
        # those breaks no live caller.
        #
        # REFUSAL, NOT REVOCATION: nothing here touches an existing token.
        if not license.accepts_new_token():
            logger.warning(
                "Refusing to mint token for licence %s: effective status %s (stored %s, "
                "deleted_at=%s) — a new credential on a dead licence would outlive it",
                license_id,
                license.effective_status(),
                license.status,
                license.deleted_at,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "license_not_active",
                    **_license_state_detail(license_id, license),
                },
            )

        # Calculate expiration.
        # `is not None`, NOT truthiness: `if expires_days:` sent 0 down the same
        # branch as None and minted a token with expires_at=NULL — which
        # Token.is_valid() reads as "never expires" (models.py: "NULL = until
        # revoked"). A caller asking for a ZERO-lifetime credential got a
        # PERPETUAL one, behind a 201 whose `expires_at: null` is
        # indistinguishable from the documented perpetual case. Same 0-is-falsy
        # class as the quota_monthly / token_budget_monthly guards.
        # ge=1 on the Query above now rejects 0 and negatives at the edge (422);
        # this `is not None` keeps the branch correct on its own terms so
        # relaxing the constraint later cannot silently resurrect the bug.
        expires_at = None
        if expires_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=expires_days)

        # ⛔ REGISTERED CALLERS THAT CANNOT PASS AN EXPIRY (licenses#244 F2):
        # commonpractice's provisioning SA mints and stores expiry-less `api`
        # tokens that its request gate introspects, so a silent 365-day default
        # would 402 every onboarded tenant a year later. Keyed on the VERIFIED
        # JWT `(iss, azp)` (LONG_LIVED_IMPLICIT_CLIENTS), never on a request field or
        # header. ⛔ Keyed on the verified (iss, azp) PAIR, iss pinned to the
        # secure-apps realm: a same-named client in another trusted realm (portal)
        # must NOT qualify. Only an `api` mint with no expiry, no `long_lived` and no
        # owner/reason fields qualifies; this replaces the license-admin
        # requirement for this implicit path only.
        implicit_registration = (
            None
            if long_lived_owner or long_lived_reason
            else implicit_long_lived_registration(
                user.iss, user.azp, token_type.value, expires_at is not None, long_lived
            )
        )
        registration = (
            LongLivedRegistration(long_lived_owner or "", long_lived_reason or "")
            if long_lived
            else implicit_registration
        )
        # ⛔ EXPIRY POLICY (licenses#244). Omitting `expires_days` no longer means
        # perpetual: `api` gets TOKEN_API_DEFAULT_EXPIRY_DAYS, and a perpetual token
        # must be a REGISTERED long-lived one. Decided HERE (one call, the same
        # function create_token enforces) so each refusal is a literal discriminator;
        # create_token re-checks at the DB boundary for every other caller.
        token_tenant_keys = frozenset({str(license.tenant_id)}) | (
            frozenset({str(token_tenant.name)}) if token_tenant is not None else frozenset()
        )
        refusal = (
            "long_lived_fields_without_long_lived"
            if not long_lived and (long_lived_owner or long_lived_reason)
            else token_expiry_refusal(
                token_type.value, expires_at is not None, registration, token_tenant_keys
            )
        )
        if refusal is not None:
            logger.warning(
                "Refusing to mint token for licence %s: expiry policy %s (type=%s)",
                license_id,
                refusal,
                token_type.value,
            )
        if refusal == "long_lived_fields_without_long_lived":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "long_lived_fields_without_long_lived",
                    "remedy": "long_lived_owner/long_lived_reason require long_lived=true",
                },
            )
        if refusal == "long_lived_requires_owner_and_reason":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "long_lived_requires_owner_and_reason",
                    "remedy": "pass non-empty long_lived_owner and long_lived_reason",
                },
            )
        if refusal == "long_lived_with_expiry":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "long_lived_with_expiry",
                    "remedy": "a long-lived token is perpetual: drop expires_days or long_lived",
                },
            )
        if refusal == "long_lived_not_permitted":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "long_lived_not_permitted",
                    "remedy": (
                        "long-lived is allowed for internal/node, and for api only on the "
                        "n8n licence-token tenants; pass expires_days instead"
                    ),
                },
            )
        if refusal is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "perpetual_token_not_registered",
                    "remedy": (
                        "pass expires_days, or long_lived=true with long_lived_owner and "
                        "long_lived_reason"
                    ),
                },
            )

        # Create token (create_token applies the same policy: an `api` mint without an
        # expiry gets the bounded default; a registered long-lived mint stays perpetual).
        token, raw_token = db_manager.create_token(
            license_id=UUID(license_id),
            token_type=token_type.value,  # Convert enum to string
            expires_at=expires_at,
            long_lived=registration,
        )
        if registration is not None:
            logger.warning(
                "long_lived_token_registered license_id=%s token_id=%s type=%s owner=%s "
                "actor=%s iss=%s azp=%s implicit=%s",
                license_id,
                token.id,
                token.token_type,
                token.long_lived_owner,
                user.preferred_username,
                user.iss,
                user.azp,
                registration.implicit_client is not None,
            )

        logger.info(f"Token created: {token.id} for license {license_id}")

        return TokenCreateResponse(
            token_id=str(token.id),
            license_id=str(token.license_id),
            token=raw_token,
            token_type=str(token.token_type),
            created_at=token.created_at.isoformat(),
            expires_at=token.expires_at.isoformat() if token.expires_at else None,
            long_lived_owner=(
                str(token.long_lived_owner) if token.long_lived_owner is not None else None
            ),
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "create_license_token: Invalid UUID"
        ) from e
    # Top-level handler: spans DB + token mint + datetime; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create token",
            e,
            "Failed to create token",
        ) from e


def _same_tenant_id(header_value: str, licence_tenant_id: object) -> bool:
    """True iff ``header_value`` names ``licence_tenant_id`` (UUID-compared; non-UUID ⇒ False).

    The raw header is never logged or echoed: in this fleet ``X-Tenant-Id`` may carry
    ``Bearer <licence token>``, i.e. a live credential.
    """
    try:
        return UUID(header_value) == UUID(str(licence_tenant_id))
    except ValueError, TypeError, AttributeError:
        return False


@app.post(
    "/licenses/{license_id}/operator-token",
    response_model=OperatorTokenResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_operator_token",
    tags=["tokens"],
    responses=COMMON_ERROR_RESPONSES,
)
def create_operator_token(
    license_id: str,
    request: Request,
    ttl_minutes: int = Query(
        15,
        ge=1,
        le=OPERATOR_TOKEN_MAX_TTL_MINUTES,
        description=f"Lifetime in minutes (1..{OPERATOR_TOKEN_MAX_TTL_MINUTES}, default 15).",
    ),
    user: User = Depends(require_role("platform-admin")),
):
    """Mint a SHORT-LIVED licence token that carries ``platform-admin`` on introspection.

    The one door by which a licence token can carry a role. It exists for operator actions
    gated on that role downstream — ingestion's cross-holder
    ``DELETE /ingest/documents/{id}?scope=document`` is the first — which read roles from
    ``/license/introspect`` and nowhere else.

    ⭐ WHERE THE AUTHORITY COMES FROM. The caller's OWN verified Keycloak JWT must carry the
    realm role ``platform-admin`` (``require_role``: a JWT realm role; a licence-token caller
    is only ever ``license-reader`` here, so an operator token can never mint another one).
    The role is recorded on this ONE token so the auth hot path never calls Keycloak.

    ⛔ YOURSELF ONLY. The licence's subject must be the caller (``subject.keycloak_sub ==
    JWT sub``). A platform admin cannot mint an admin credential that another person — or a
    service — would then hold. The licence must already exist and be active; creating one
    for the operator in a target tenant is the ordinary ``POST /licenses/user/{sub}`` path
    (``license-admin``), which is also what scopes the token to that tenant.

    ⛔ TENANT-CONSISTENT. An ``X-Tenant-Id`` header, when sent, must name the licence's own
    tenant (UUID-compared, so case/format variants of one id agree): 403 otherwise. The
    token is scoped by the LICENCE, so a mismatching header means the caller believes it is
    minting for a different tenant than the one the credential will act in — refused rather
    than silently minting for the licence's tenant. Absent header ⇒ the licence decides.

    ⛔ SHORT-LIVED. ``ttl_minutes`` is capped at 60 and the DB refuses a role-bearing token
    without an expiry ≤ created_at + 65 min. Revoke early with ``POST /license/revoke``.

    Every mint is logged at WARNING as ``operator_token_minted`` (actor, licence, tenant,
    token id, roles, expiry).
    """
    try:
        license = db_manager.get_license(UUID(license_id))
        if not license:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")
        subject = (
            db_manager.get_subject(UUID(str(license.subject_id)))
            if license.subject_id is not None
            else None
        )
        if subject is None or str(subject.keycloak_sub) != str(user.sub):
            # Same answer as "no such licence": a platform admin probing other people's
            # licence ids learns nothing about which exist.
            logger.warning(
                "operator_token_refused_not_own_licence actor_sub=%s license_id=%s",
                user.sub,
                license_id,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")
        header_tenant = (request.headers.get("X-Tenant-Id") or "").strip()
        if header_tenant and not _same_tenant_id(header_tenant, license.tenant_id):
            logger.warning(
                "operator_token_refused_tenant_mismatch actor_sub=%s license_id=%s "
                "license_tenant_id=%s",
                user.sub,
                license_id,
                license.tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="X-Tenant-Id does not match the licence's tenant",
            )
        if not license.is_active():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="refusing to mint: licence is not active",
            )
        token_tenant = db_manager.get_tenant(str(license.tenant_id))
        if token_tenant is not None and token_tenant.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="refusing to mint: tenant is archived",
            )

        roles = [r for r in OPERATOR_GRANTABLE_ROLES if user.has_realm_role(r)]
        if not roles:  # pragma: no cover - require_role above already guarantees one
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No grantable role")
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        token, raw_token = db_manager.create_token(
            license_id=UUID(license_id),
            token_type=TokenType.api.value,
            expires_at=expires_at,
            granted_roles=roles,
        )
        logger.warning(
            "operator_token_minted actor_sub=%s actor=%s license_id=%s tenant_id=%s "
            "token_id=%s roles=%s expires_at=%s",
            user.sub,
            user.preferred_username,
            license_id,
            license.tenant_id,
            token.id,
            ",".join(roles),
            expires_at.isoformat(),
        )
        return OperatorTokenResponse(
            token_id=str(token.id),
            license_id=str(token.license_id),
            token=raw_token,
            token_type=str(token.token_type),
            created_at=token.created_at.isoformat(),
            expires_at=token.expires_at.isoformat() if token.expires_at else None,
            roles=list(roles),
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "create_operator_token: Invalid UUID"
        ) from e
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create operator token",
            e,
            "Failed to create operator token",
        ) from e


# ============= App Endpoints =============


@app.post(
    "/apps",
    response_model=AppResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_app",
    tags=["apps"],
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {
            "model": AppResponse,
            "description": "Existing app returned unchanged (idempotent — key already registered)",
        },
        400: {
            "model": ErrorResponse,
            "description": (
                "Nothing was created: `owner_not_found` (`detail.error`) — a sent issuer is "
                "not a tenant_owner key. Also: malformed body."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "Nothing was created: `unknown_api_client` — the calling client is not "
                "registered as an owning app (api_client); `issuer_not_caller_owner` — a "
                "sent issuer is not the caller's own owner (only a cross_owner client may "
                "name another owner, and that is audited)."
            ),
        },
    },
)
def create_app(app_data: AppCreate, user: User = Depends(require_role("license-admin"))):
    """
    Create a new app (idempotent — returns existing app if key matches)

    Requires 'license-admin' role
    """
    try:
        existing = db_manager.get_app_by_key(app_data.key)
        if existing:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=AppResponse(**existing.to_dict()).model_dump(),
            )

        caller_owner = _require_known_owner(user)
        issuers = app_data.issuers or [caller_owner]
        # Issuer squatting: naming ANOTHER owner as issuer would let any
        # license-admin client hand that owner (or deny it) an app it never
        # registered. Only the caller's own owner may be named; cross_owner
        # (break-glass) keeps full freedom, audited.
        foreign = sorted({o for o in issuers if o != caller_owner})
        if foreign:
            if not user.cross_owner:
                logger.warning(
                    "issuer_not_caller_owner: refused app create key=%s owner=%s issuers=%s azp=%s",
                    app_data.key,
                    caller_owner,
                    ",".join(foreign),
                    user.azp,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "issuer_not_caller_owner",
                        "issuers": foreign,
                        "message": (f"owner {caller_owner!r} may only name itself as an issuer"),
                    },
                )
            _audit_cross_owner(
                user, "app_create_foreign_issuers", None, f"key={app_data.key} issuers={foreign}"
            )
        unknown = [o for o in issuers if not db_manager.owner_exists(o)]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "owner_not_found",
                    "owner_key": unknown[0],
                    "message": f"issuer(s) are not tenant_owner keys: {', '.join(unknown)}",
                },
            )
        app = db_manager.create_app(
            key=app_data.key,
            name=app_data.name,
            description=app_data.description,
            audience=app_data.audience,
            issuers=issuers,
        )

        logger.info(f"App created: {app.id} ({app.key})")

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=AppResponse(**app.to_dict()).model_dump(),
        )

    except HTTPException:
        raise
    except IntegrityError:
        # Lost the read-then-insert race on the unique ``apps.key`` index
        # (models.py: ``key = Column(..., unique=True)``). The read above and the
        # insert are two separate sessions with no lock between them, so a
        # concurrent POST for the same key can commit in the gap — reachable
        # today across the two replicas, and routine once handlers run in the
        # threadpool instead of being serialised by the event loop.
        #
        # 200-with-the-existing-row, not 409: this route's docstring promises
        # "idempotent — returns existing app if key matches", and the caller
        # cannot tell whether it lost the race or arrived second. Same idiom as
        # ``create_tenant``. Re-raise if the re-read finds nothing, because then
        # the violated constraint was NOT the key race and returning 200 would
        # be a lie.
        existing = db_manager.get_app_by_key(app_data.key)
        if existing is not None:
            logger.info(f"App {existing.id} ({existing.key}) already exists; returning it")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=AppResponse(**existing.to_dict()).model_dump(),
            )
        raise
    # Top-level handler: spans DB + JSONResponse build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to create app", e, "Failed to create app"
        ) from e


#: The API vocabulary for ``tenants.budget_currency`` (migration 043, PLAN REV F).
#: A Literal, so every future currency is an explicit, reviewed addition; pinned equal
#: to ``models.BUDGET_CURRENCIES`` (and so to ``ck_tenants_budget_currency``) by
#: tests/unit/test_migration_043_budget_currency.py.
BudgetCurrency = Literal["EUR", "USD", "CAD"]

#: The API vocabulary for ``tenants.kind`` (migration 027, ``ck_tenants_kind``). A
#: Literal, so an unknown kind is a 422 at the edge rather than an IntegrityError at
#: the INSERT; pinned equal to ``models.TENANT_KINDS`` by tests/unit/test_tenants.py.
TenantKind = Literal["customer", "internal", "service", "test", "unclassified"]

#: The owner capability that may SWITCH a tenant's budget currency mid-month. The
#: same realm role that gates the operator-token mint (``platform-admin``); no new
#: role is introduced for it.
BUDGET_CURRENCY_CHANGE_ROLE = "platform-admin"

#: Header a trusted relay (provisioning-backend, holding ``license-admin``) uses to
#: forward the END USER's portal JWT on ``PUT /tenants/{id}/budget-currency``, so
#: the owner can switch currency without the relay holding ``platform-admin``.
FORWARDED_USER_AUTH_HEADER = "X-Forwarded-User-Authorization"

#: Realm roles on a VERIFIED forwarded portal token that carry the owner capability.
BUDGET_CURRENCY_FORWARDED_OWNER_ROLES = frozenset({"super_admin", "platform-admin"})


class TenantResponse(BaseModel):
    """A canonical tenant identity, exactly as ``Tenant.to_dict`` emits it.

    Both tenant writers returned a raw ``JSONResponse`` with no ``response_model``,
    so the published schema for them was a bare ``{}`` — orval emits `unknown`/`any`
    for the response of the endpoint the provisioning service calls as step ONE of
    post-sales onboarding.

    Three routes carry this model, and they are NOT equally enforced.
    ``create_tenant`` and ``register_tenant`` still return a ``JSONResponse``,
    because each answers 200 OR 201 depending on whether the row already existed —
    for those two the declaration is documentation only, since FastAPI skips
    ``response_model`` entirely for a returned ``Response``. ``set_tenant_byo_mode``
    has a single 200 arm and so returns the plain dict, which FastAPI serialises
    THROUGH this model: there the schema is enforced, not merely published.

    Declared HERE, above the byo-mode routes, rather than next to ``TenantCreate``
    further down: decorators are evaluated at import, so the class has to exist
    before the first route that names it.
    """

    id: str = Field(..., description="Canonical tenant id (UUIDv7).")
    name: str = Field(..., description="Unique tenant name, e.g. 'acme-corp'.")
    keycloak_realm: str | None = Field(
        None, description="Keycloak realm this tenant authenticates in, if any."
    )
    byo_mode: str | None = Field(None, description="Bring-your-own mode for this tenant.")
    # ⚠️ `kind` and `cost_centre` were MISSING here while `Tenant.to_dict` has
    # emitted both since migration 027 — so this model's own first line ("exactly
    # as Tenant.to_dict emits it") was false, and on the one route FastAPI
    # actually enforces it through (`set_tenant_byo_mode`, a plain dict) both
    # fields were silently STRIPPED from the response. That went unnoticed while
    # nothing could write `cost_centre`; the moment there is a route that sets it,
    # a caller reading its own 200 back would not see the value it just wrote.
    kind: str | None = Field(
        None,
        description="Tenant classification: customer | internal | service | test | unclassified.",
    )
    cost_centre: str | None = Field(
        None,
        description=(
            "Chargeback join key, or null when the apportionment is undecided. "
            "Constrained to a closed vocabulary by ck_tenants_cost_centre "
            "(migration 034); set it with PATCH /tenants/{name_or_id}/cost-centre."
        ),
    )
    budget_currency: BudgetCurrency | None = Field(
        None,
        description=(
            "null = the tenant's budget is TOKENS (license_sync token budgets pushed to "
            "billing per licence). 'EUR' / 'USD' / 'CAD' = its budget is MONEY held by "
            "billing in that currency, so this service pushes NO token budget for it — "
            "only request quotas (migration 043). Set via "
            "PUT /tenants/{name_or_id}/budget-currency; never returns to null."
        ),
    )
    archived_at: str | None = Field(
        None,
        description=(
            "ISO-8601 timestamp this tenant was retired, or null if live. Archiving "
            "hides a tenant from the operator listing and signals that new provisioning "
            "should not target it; it revokes NOTHING — licences, tokens, budgets and "
            "role grants all survive and existing tokens keep authenticating."
        ),
    )
    owner_key: str | None = Field(
        None,
        description=(
            "The app that OWNS this tenant (migration 050), e.g. 'fr', "
            "'commonpractice', 'platform'. Tenant names are unique per owner."
        ),
    )
    external_ref: str | None = Field(
        None,
        description="The owning app's own stable id for this tenant, or null.",
    )
    created_at: str | None = Field(None, description="ISO-8601 creation timestamp.")


class TenantRename(BaseModel):
    """Change a tenant's human NAME. The UUID is the identity and never moves."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        max_length=255,
        description=(
            "New unique tenant name. NB `name` is a resolver key — license-authz "
            "translates an incoming tenant name to the canonical UUID — so a rename "
            "re-points every caller that addresses this tenant BY NAME. Callers "
            "holding the UUID are unaffected."
        ),
    )


class TenantBudgetCurrencyUpdate(BaseModel):
    """Mark a tenant's budget as money (migration 043). ``{"budget_currency": "EUR"}``."""

    budget_currency: BudgetCurrency = Field(
        ...,
        description=(
            "'EUR', 'USD' or 'CAD' marks the tenant's budget as MONEY in billing in that "
            "currency: from then on this service pushes NO token budget for it, only "
            "request quotas (migration 043). A Literal so further currencies are explicit "
            "additions; there is no value meaning 'back to tokens'."
        ),
    )


class TenantArchiveUpdate(BaseModel):
    """Retire a tenant, or restore one."""

    archived: bool = Field(
        ...,
        description=(
            "true retires the tenant, false restores it. Idempotent both ways, and "
            "re-archiving keeps the ORIGINAL timestamp rather than moving it."
        ),
    )


class TenantByoModeUpdate(BaseModel):
    """Set a tenant's BYO LLM entitlement."""

    byo_mode: str = Field(..., description="'local_allowed', 'byo_required', or 'platform_only'")


class TenantCostCentreUpdate(BaseModel):
    """Set — or clear — a tenant's chargeback cost centre.

    The body is ``{"cost_centre": <value>}``. ``null`` is a MEANINGFUL value here
    and clears the field: "this tenant's apportionment is undecided" is a state
    the column has to be able to express, and four production tenants are in it
    deliberately.
    """

    cost_centre: str | None = Field(
        ...,
        max_length=64,
        description=(
            "Chargeback join key. One of: "
            + ", ".join(f"'{c}'" for c in COST_CENTRES)
            + " — or null to clear it. The vocabulary is CLOSED (ck_tenants_cost_centre, "
            "migration 034) because this is a join key: an unconstrained value cannot be "
            "told apart from a typo of an existing centre, and a typo produces a "
            "chargeback bucket of one rather than an error. Adding a centre means "
            "editing the CHECK, models.COST_CENTRES and this list together."
        ),
    )


class TenantByoModeResponse(BaseModel):
    """The resolved BYO posture for a tenant, optionally scoped to one product.

    Exactly the dict ``get_tenant_byo_mode`` builds. ``app`` echoes the ``?app=``
    query parameter, so it is null on the tenant-wide lookup.
    """

    tenant_id: str = Field(..., description="Canonical tenant id (UUIDv7).")
    name: str = Field(..., description="Tenant name.")
    byo_mode: str = Field(
        ...,
        description=(
            "'local_allowed', 'byo_required', or 'platform_only'. With ``app`` set this is the "
            "per-product override when one exists, else the tenant-wide value."
        ),
    )
    app: str | None = Field(
        None, description="The product key the posture was resolved for; null when unscoped."
    )
    resolution: str = Field(
        RESOLUTION_ROW,
        description=(
            "WHERE ``byo_mode`` came from — `row` (read from the tenant/override row) or "
            "`fault` (the read failed and this is the fail-closed FABRICATION, not a "
            "verdict). ⛔ `fault` is only reachable when the caller passed "
            "`on_unresolved=fail_closed`; the default answers 503 instead. A caller that "
            "PERSISTS or BILLS this value must treat `fault` as 'unknown, ask again' — "
            "`byo_required` is identical in both cases, which is exactly why the string "
            "alone was not enough and one database blip could be frozen into an invoice."
        ),
    )


class TenantOfferingRecord(BaseModel):
    """One per-(tenant, product) posture row, with the offering that produced it.

    ⚠️ ``offering_code: null`` does NOT mean "no offering". It means the
    provenance is unknown — the posture was set directly via
    ``PUT /tenants/{n}/apps/{app_key}/byo-mode``, or it predates migration 025,
    which added the column and could not backfill it because the offering code
    had never been recorded anywhere. Render it as unknown, not as blank: a blank
    cell asserts "nothing provisioned" about rows that may well have been.
    """

    app_key: str = Field(..., description="Product key (App.key), e.g. 'w2v-image'.")
    app_name: str = Field(..., description="Human-readable product name.")
    byo_mode: str = Field(
        ..., description="'local_allowed', 'byo_required', or 'platform_only' for this product."
    )
    offering_code: str | None = Field(
        None,
        description=(
            "The offering that produced this posture, or null when the provenance is "
            "unknown (set directly, or predates migration 025). NOT 'no offering'."
        ),
    )
    created_at: str | None = Field(None, description="When the override row was created.")
    updated_at: str | None = Field(None, description="When it was last written.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def offering_provenance(self) -> str:
        """``"recorded"`` or ``"unknown"`` — the null above, said out loud.

        The docstrings have warned since migration 025 that ``offering_code:
        null`` means UNKNOWN and not "no offering". A docstring cannot reach the
        consumer: over the wire a caller sees ``null`` in a field named
        ``offering_code`` and reasonably concludes no offering exists. Every
        renderer then has to re-derive the distinction from prose, and the safe
        reading is the wrong one.

        So the payload states it. ``"unknown"`` is not a value anyone renders as
        blank by accident, which ``null`` is.

        Derived, never stored: it is exactly ``offering_code is not None``, so it
        cannot drift from the column it describes.
        """
        return "recorded" if self.offering_code is not None else "unknown"


class TenantAppByoModeResponse(BaseModel):
    """A per-(tenant, product) BYO posture override, as ``set_tenant_app_byo_mode`` returns it."""

    tenant_id: str = Field(..., description="Canonical tenant id (UUIDv7).")
    tenant_name: str = Field(..., description="Tenant name.")
    app_id: str = Field(..., description="App id (UUID).")
    app_key: str = Field(..., description="Product key (App.key), e.g. 'w2v-image'.")
    byo_mode: str = Field(..., description="The posture just written for this (tenant, product).")
    offering_code: str | None = Field(
        None,
        description=(
            "The offering that produced this posture, or null when it was set directly "
            "rather than by provisioning an offering."
        ),
    )
    prior_offering_code: str | None = Field(
        None,
        description=(
            "The offering code this write OVERWROTE, or null when there was none. Restored "
            "alongside ``prior_byo_mode`` on a provision rollback."
        ),
    )
    prior_byo_mode: str | None = Field(
        ...,
        description=(
            "The override this upsert OVERWROTE, or null when it created the row. "
            "This is what makes the write undoable — see provision_tenant_offering's rollback."
        ),
    )


class TenantAppByoModeClearResponse(BaseModel):
    """Result of removing a per-(tenant, product) BYO posture override. Idempotent."""

    tenant: str = Field(..., description="The tenant name or id exactly as it was addressed.")
    app_key: str = Field(..., description="Product key (App.key).")
    cleared: bool = Field(
        ..., description="True if an override row was deleted; false if there was none."
    )


@app.patch(
    "/tenants/{name_or_id}/byo-mode",
    response_model=TenantResponse,
    operation_id="set_tenant_byo_mode",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
def set_tenant_byo_mode(
    name_or_id: str,
    body: TenantByoModeUpdate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Set a tenant's BYO LLM entitlement (commercial tenants → 'byo_required').

    Requires 'license-admin' or 'license-manager', and the caller must OWN the
    tenant (a grantee gets 403 ``tenant_owner_only``). The value is surfaced via token introspection
    so worker_management can hard-fail runs for byo_required tenants that have
    not configured their own provider.
    """
    _sel = str(_resolve_owned_tenant(user, name_or_id, "tenant_byo_mode").id)
    if body.byo_mode not in ("local_allowed", "byo_required", "platform_only"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="byo_mode must be 'local_allowed', 'byo_required', or 'platform_only'",
        )
    tenant = db_manager.set_tenant_byo_mode(_sel, body.byo_mode)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    logger.info(f"Tenant byo_mode set: {tenant.id} ({tenant.name}) -> {body.byo_mode}")
    # Plain dict, NOT JSONResponse: this route has a single 200 arm and no custom
    # header, and a returned Response makes FastAPI skip `response_model` entirely —
    # the declaration would document a contract nothing enforces.
    return tenant.to_dict()


@app.patch(
    "/tenants/{name_or_id}/cost-centre",
    response_model=TenantResponse,
    operation_id="set_tenant_cost_centre",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
def set_tenant_cost_centre(
    name_or_id: str,
    body: TenantCostCentreUpdate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Set or clear a tenant's chargeback cost centre. Requires 'license-admin' or
    'license-manager' on a tenant the caller OWNS (grantee: 403 ``tenant_owner_only``).

    ⛔ THIS CLOSES A WRITE PATH THAT DID NOT EXIST. ``cost_centre`` was
    INSERT-ONLY: ``POST /tenants`` and ``PUT /tenants/{tenant_id}`` both accept it
    and both apply it only when creating a NEW row — ``register_tenant`` returns
    an existing tenant UNCHANGED by design, so re-registering could never
    overwrite an operator's choice — while ``PATCH /tenants/{name_or_id}``
    renames and nothing else. A tenant created without a cost centre therefore
    could not be given one through this API at all; the four rows measured NULL
    on 2026-09-08 (`default-anon`, `george-dekker`, `nonagentic`, `portal`) were
    reachable only by direct SQL against the primary.

    It is a SEPARATE sub-route rather than a field on ``PATCH /tenants/{id}``,
    mirroring ``/byo-mode`` and ``/archive``: ``TenantRename.name`` is required,
    so folding an optional ``cost_centre`` into that body would have made the
    rename optional too, and one endpoint would then decide between two unrelated
    intents from which keys happened to be present.

    ⚠️ ``{"cost_centre": null}`` CLEARS the value and is a deliberate operation,
    not a malformed request. An operator who set the wrong centre must be able to
    return the row to "undecided" rather than being made to choose a second wrong
    answer — this service never guesses an apportionment.

    ⚠️ This endpoint does NOT decide which centre a tenant belongs to, and it must
    not be driven by a heuristic. `platform-core` and `system` currently hold
    kind='internal' with cost_centre='service', which is an inconsistency an
    operator has to resolve; nothing here will resolve it for them.
    """
    _sel = str(_resolve_owned_tenant(user, name_or_id, "tenant_cost_centre").id)
    # Validated in BOTH places on purpose. Pydantic cannot see COST_CENTRES as an
    # enum without freezing the vocabulary into the generated client, and
    # `set_tenant_cost_centre` is reachable from `scripts/` with no Pydantic model
    # in the way — so the method validates too, and this arm turns its
    # ClientSafeError into the 422 a caller can act on.
    try:
        tenant = db_manager.set_tenant_cost_centre(_sel, body.cost_centre)
    except ClientSafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    logger.info("Tenant cost_centre set: %s (%s) -> %s", tenant.id, tenant.name, body.cost_centre)
    # Plain dict, NOT JSONResponse — see set_tenant_byo_mode.
    return tenant.to_dict()


def _verify_forwarded_user_token(header_value: str) -> tuple[dict[str, Any], str]:
    """Verify ``X-Forwarded-User-Authorization`` or raise 401; return ``(claims, realm)``.

    Delegates to ``auth.jwt_verify.verify_user_jwt`` — the service's one
    JWKS verifier, narrowed to the ``USER_TRUSTED_ISSUERS`` allowlist (default:
    the ``portal`` realm only). The token and the verifier's exception text are
    never logged or echoed.
    """
    scheme, _, token = header_value.strip().partition(" ")
    token = token.strip()
    try:
        if scheme.lower() != "bearer" or not token or not looks_like_jwt(token):
            raise InvalidTokenError("not a Bearer JWT")
        return verify_user_jwt(token)
    except InvalidTokenError as exc:
        logger.warning("Refused %s: %s", FORWARDED_USER_AUTH_HEADER, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "forwarded_user_token_invalid",
                "message": f"{FORWARDED_USER_AUTH_HEADER} could not be verified",
            },
        ) from exc


def _enforce_forwarded_realm_owner(realm: str, tenant_id: str) -> None:
    """Migration 072: a forwarded user from an app-owned realm may act only on that
    app's tenants (owned or granted). BLOCKING. 403 ``realm_owner_mismatch``; a
    resolver fault is 503 ``realm_owner_unresolved`` (never treated as "no row")."""
    try:
        owner = db_manager.resolve_realm_owner(realm)
    except Exception as exc:
        logger.warning("realm_owner_resolution_failed: realm=%s (refused)", realm, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "realm_owner_unresolved",
                "message": "the forwarded token realm's owner could not be resolved",
            },
        ) from exc
    if not realm_owner_permits(owner, tenant_id):
        logger.warning(
            "realm_owner_mismatch: forwarded realm=%s realm_owner=%s tenant=%s",
            realm,
            owner,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=realm_owner_mismatch_detail()
        )


@app.put(
    "/tenants/{name_or_id}/budget-currency",
    response_model=TenantResponse,
    operation_id="set_tenant_budget_currency",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        401: {
            "model": ErrorResponse,
            "description": (
                "`forwarded_user_token_invalid` — an `X-Forwarded-User-Authorization` "
                "header was sent but is not `Bearer <JWT>`, or the JWT fails "
                "signature / expiry / issuer verification, or its issuer is not in "
                "`USER_TRUSTED_ISSUERS` (unset: the `portal` realm only). Refused "
                "outright; the call is never downgraded to the caller's own authority. "
                "Nothing written."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "Forbidden; nothing written. The caller lacks the required role, or "
                "`detail.error` is `realm_owner_mismatch` (migration 072): the forwarded "
                "token's Keycloak realm is owned by an app (`realm_owner`) that neither "
                "owns this tenant nor holds an active grant on it."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`budget_currency_immutable` — the tenant already holds a DIFFERENT "
                "budget_currency and the owner capability is absent: neither the "
                "caller holds realm role `platform-admin`, nor a `license-admin` "
                "caller forwarded a verified portal token carrying `super_admin` or "
                "`platform-admin`. Also 409 `tenant_archived` for an archived tenant. "
                "Nothing written."
            ),
        },
    },
)
def set_tenant_budget_currency(
    name_or_id: str,
    body: TenantBudgetCurrencyUpdate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES, BUDGET_CURRENCY_CHANGE_ROLE)),
    forwarded_user_authorization: str | None = Header(
        None,
        alias=FORWARDED_USER_AUTH_HEADER,
        description=(
            "Optional `Bearer <portal JWT>` of the END USER on whose behalf a "
            "`license-admin` relay (provisioning-backend) calls. Verified with the "
            "same JWKS checks as `Authorization`; its issuer must be in "
            "`USER_TRUSTED_ISSUERS` (unset: the `portal` realm only), and a realm with "
            "a `realm_owner` row may only name that owner's (or granted) tenants. "
            "If its realm roles include `super_admin` or "
            "`platform-admin`, the currency may be SWITCHED, and the audit line "
            "names that user as `actor` and the caller's `azp` as `via`. An "
            "invalid/expired/unlisted-issuer token is a 401, never ignored."
        ),
    ),
):
    """Set a tenant's money-budget currency (PLAN §2.2, REV F).

    Requires 'license-admin' or 'license-manager' (same as ``/byo-mode``) or the
    owner role ``platform-admin``, on a tenant the caller OWNS (grantee: 403
    ``tenant_owner_only``). A dedicated sub-route like ``/byo-mode`` and
    ``/cost-centre``; ``PATCH /tenants/{name_or_id}`` stays the rename. PUT because
    the call is idempotent: re-sending the value the tenant already holds is a 200
    no-op.

    * null -> 'EUR' / 'USD' / 'CAD': either role.
    * a currency -> a DIFFERENT currency: the OWNER capability only (owner ruling
      2026-10-02: every provisioning decision is updatable during a running month),
      which is EITHER the caller's own realm role ``platform-admin``, OR a
      ``license-admin`` relay (provisioning-backend) forwarding the owner's portal
      JWT in ``X-Forwarded-User-Authorization`` whose verified realm roles include
      ``super_admin`` or ``platform-admin``. The relay itself is never granted
      ``platform-admin`` (least privilege). Otherwise 409
      ``budget_currency_immutable``. A forwarded token that fails verification or is
      not from the ``portal`` realm is a 401 — never ignored. Converting
      the remaining money budget at the current FX rate is billing's job; this
      service only records which currency the budget is held in.
    * back to null: impossible — the body cannot express it (422).
    * an ARCHIVED tenant: 409 ``tenant_archived`` (same refusal as bundle ensure).

    Every write is audited by a ``budget_currency_changed`` log line (``actor`` =
    forwarded owner sub, else caller sub; ``via`` = caller ``azp``; tenant, from,
    to, when; never a token); this service has no audit table.

    From the moment a tenant has a currency, ``_provision_budget`` and the nightly
    metering reconcile push NO license_sync token budget for it (quota only),
    whatever the currency. Provisioning sets this BEFORE minting any licence.

    ⚠️ EXISTING TENANTS: this flag only changes what is pushed FROM NOW ON. Marking
    a tenant that already holds metered licences does NOT retract the license_sync
    token budgets already pushed for them — those stay ACTIVE in billing.
    Converting them is a separate, owner-approved step (PLAN Phase 2, estate
    conversion), never a side effect of this call.
    """
    _sel = str(_resolve_owned_tenant(user, name_or_id, "tenant_budget_currency").id)
    actor = user.sub
    allow_change = user.has_realm_role(BUDGET_CURRENCY_CHANGE_ROLE)
    if forwarded_user_authorization is not None:
        # Verified whenever PRESENT, whoever the caller is: a bad forwarded token
        # is a refusal, never silently dropped (that would let a relay bug or a
        # forgery degrade into "acting as the SA" with no signal).
        owner_claims, owner_realm = _verify_forwarded_user_token(forwarded_user_authorization)
        _enforce_forwarded_realm_owner(owner_realm, _sel)
        actor = str(owner_claims["sub"])
        owner_roles = (owner_claims.get("realm_access") or {}).get("roles") or []
        if user.has_realm_role("license-admin") and any(
            r in BUDGET_CURRENCY_FORWARDED_OWNER_ROLES for r in owner_roles
        ):
            allow_change = True
    # Neither arm echoes the caught exception: both details are fixed strings.
    try:
        tenant = db_manager.set_tenant_budget_currency(
            _sel,
            body.budget_currency,
            actor=actor,
            via=user.azp,
            allow_change=allow_change,
        )
    except BudgetCurrencyTenantArchivedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_archived",
                "message": "an archived tenant's budget_currency is not changed",
            },
        ) from exc
    except BudgetCurrencyImmutableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "budget_currency_immutable",
                "message": (
                    "budget_currency may only move from null to a currency; switching "
                    "currency requires the owner capability (platform-admin, or a "
                    "forwarded portal super_admin token via a license-admin relay)"
                ),
            },
        ) from exc
    except ClientSafeError as exc:  # unreachable via the Literal body; defence in depth
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid budget_currency"
        ) from exc
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    logger.info(
        "Tenant budget_currency set: %s (%s) -> %s", tenant.id, tenant.name, body.budget_currency
    )
    # Plain dict, NOT JSONResponse — see set_tenant_byo_mode.
    return tenant.to_dict()


@app.get(
    "/tenants/{name_or_id}/byo-mode",
    response_model=TenantByoModeResponse,
    operation_id="get_tenant_byo_mode",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        400: {
            "model": ErrorResponse,
            "description": (
                "`malformed_tenant_selector` — the path selector is neither a UUID nor a "
                "legal tenant name (`^[a-z0-9][a-z0-9-]*$`), so no tenant could ever "
                "match it. This is a CLIENT fault and is deliberately NOT a 404: a 404 "
                "from this route is read downstream as 'this tenant is not in the "
                "registry', which billing maps to `byo_required` = commercial. A typo "
                "must not be able to produce a commercial verdict."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "`tenant_not_found_in_registry` — the selector is well-formed and no "
                "`tenants` row matches it. STABLE and machine-readable, because this "
                "status has three possible meanings on this path and only this one is "
                "the tenant's absence: a malformed selector now answers 400 (above) and "
                "an unmatched ROUTE answers FastAPI's own 404, whose body carries "
                '`detail: "Not Found"` and no `error` key. Branch on the literal, never '
                "on the status alone."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`byo_mode_unresolved` — the posture could not be DETERMINED (the tenant "
                "row read faulted on both the first attempt and its retry). NOT a "
                "verdict: the fail-closed `byo_required` this service uses internally is "
                "a fabrication, and returning it as a 200 let billing cache an "
                "infrastructure blip as a durable commercial fact. Retry. Access-gate "
                "callers that must keep denying on a fault pass "
                "`on_unresolved=fail_closed` — see the route docstring."
            ),
        },
    },
)
def get_tenant_byo_mode(
    name_or_id: str,
    app: str | None = None,
    on_unresolved: str = Query(
        # ⛔ DEFAULT IS `fail_closed` = TODAY'S BEHAVIOUR, deliberately, and the
        # reasoning is worth keeping because the first draft shipped `error` and
        # it would have been a net REGRESSION.
        #
        # The change was justified as "billing caches a commercial verdict from an
        # infra fault". Measured, that is FALSE for billing: `services/byo_mode.py`
        # calls `GET /tenants/{id}/byo-mode` with NO `app` parameter (zero
        # occurrences of `app=`/`params=` in that module), so it takes the
        # tenant-wide branch — which `get_tenant` has ALWAYS answered 5xx on,
        # because it does not swallow faults. Billing was never exposed to this.
        #
        # The ONLY `?app=` caller is `worker_management`
        # (`src/api/auth_middleware.py:381`), and it FAILS OPEN on every non-200,
        # caching only 200s. So defaulting to `error` would have moved that auth
        # middleware from "caches a fail-closed guess for 300s" to "allows the
        # request" during a licences database fault — a security regression in
        # another service's auth path, bought for zero benefit to billing.
        #
        # ⇒ The 503 is available to any caller that wants it, opt-in. Flip this
        # default to `error` only AFTER worker_management passes
        # `on_unresolved=fail_closed` explicitly; the docstring below and the ⛔
        # note at the fault branch both say so.
        "fail_closed",
        pattern="^(error|fail_closed)$",
        description=(
            "What to do when the posture cannot be DETERMINED (a database fault, not "
            "a missing tenant). `fail_closed` is the DEFAULT and is today's "
            "behaviour: it answers 200 with the fabricated `byo_required` and "
            '`resolution: "fault"`, for a LIVE ACCESS GATE that must keep denying '
            "through a blip and reads only `byo_mode`. `error` is OPT-IN and answers "
            "503 `byo_mode_unresolved`, so a caller that PERSISTS the answer retries "
            "instead of freezing a guess — choose it if you cache or invoice on this. "
            "⚠️ The default is not `error` because the only caller passing `app` is "
            "worker_management, which fails OPEN on every non-200."
        ),
    ),
):
    """Return a tenant's BYO entitlement by UUID or name (internal S2S lookup).

    Intentionally tokenless FOR UUID SELECTORS ONLY — and ONE regex plus one
    shape test are the only things keeping it so. ``/tenants`` **is** in
    ``PROTECTED_PREFIXES``; this GET stays open solely because
    ``_BYO_MODE_OPEN_ROUTES`` is matched first in ``AuthMiddleware.dispatch``,
    ahead of the protected-prefix check, and only when ``name_or_id`` parses as
    a UUID (``_is_tenant_uuid``). A NAME selector takes the normal auth path: it
    401s without a valid bearer and resolves as before with one. Narrow or drop
    that regex and this route 401s for the S2S callers too. The write variants
    (PATCH here, PUT and DELETE on the per-app shape) are NOT exempt and keep
    their ``license-admin`` gate.

    worker_management's trusted-network auth path calls this to enforce
    byo_required on S2S worker runs that carry no license token (the gap that let
    byo_required tenants reach the platform LLM via requirements/kag/coding).

    Why a 401 here is worse than an outage: that caller FAILS OPEN. A 401 is a
    successful HTTP response, so it does not even reach its ``except`` — it falls
    past the ``status_code == 200`` check and returns ``local_allowed`` with no log
    line at all. The symptom is therefore not a broken worker run; it is
    byo_required enforcement silently disappearing fleet-wide while every test
    stays green.

    ``?app=<key>`` (optional): resolve the PER-PRODUCT posture for that app —
    returns the ``tenant_app_byo_mode`` override when one exists, else falls
    back to the tenant-wide value. Omitting ``app`` returns the tenant-wide
    ``byo_mode`` (unchanged, backward compatible).

    ────────────────────────────────────────────────────────────────────────────
    ⛔ THIS ROUTE SERVES TWO CALLERS THAT WANT OPPOSITE THINGS FROM A FAULT, AND
    THE SPLIT BELOW IS WHY IT NO LONGER HAS TO GUESS WHICH ONE IS ASKING.

      * worker_management's trusted-network path (``src/api/auth_middleware.py``)
        is a LIVE ACCESS GATE. It reads only ``byo_mode``, caches a 200 for 300s,
        and FAILS OPEN on every non-200 — so for it a 503 is strictly worse than
        a fabricated denial.
      * billing reads the same route to decide INVOICEABILITY, treats a 200 as a
        durable answer, and freezes it into an immutable invoice. For it a
        fabricated denial is the worst possible reply: ``byo_required`` means
        COMMERCIAL, so one database blip bills an internal tenant commercially
        and there is no 5xx anywhere to retry on.

    Before this change the ``?app=`` branch resolved through ``_lookup_byo_mode``,
    which fail-closes to ``byo_required`` on a DB fault (models.py, see
    ``_FAULTED_TENANT_POSTURE``) and returned it as **200**. So the infra-fault
    answer and the real commercial answer were byte-identical, and billing had no
    way to tell them apart.

    ⚠️ THE TENANT-WIDE BRANCH WAS ALREADY INCONSISTENT WITH THAT, which is what
    makes the 503 default safe rather than novel: ``get_tenant`` does not swallow
    a DB fault, so a fault on the no-``app`` path has ALWAYS escaped as a 500.
    Only the ``?app=`` path fabricated a 200. The default below makes the two
    branches agree, on the honest side.

    Three separable outcomes, three statuses:
      * resolved                   -> 200, ``resolution: "row"``
      * no such tenant             -> 404 ``tenant_not_found_in_registry``
      * selector cannot name one    -> 400 ``malformed_tenant_selector``
      * could not be determined    -> 503 ``byo_mode_unresolved``, unless the
                                      caller passed ``on_unresolved=fail_closed``

    ⛔ WORKER_MANAGEMENT MUST ADOPT ``on_unresolved=fail_closed`` TO KEEP ITS
    CURRENT BEHAVIOUR. It is named here rather than special-cased by client
    identity because this route is auth-exempt: there is no verified caller
    identity to branch on, so "who is asking" can only be something the caller
    says. Until it does, a licences-DB fault makes that path fail OPEN (and log
    ``byo_mode_lookup_non_200``, which it already does) instead of denying for up
    to 300s. That is a deliberate, logged, uncached window — not a silent one.
    """
    lookup = db_manager.resolve_tenant_selector(name_or_id)
    if lookup.outcome == TENANT_LOOKUP_MALFORMED:
        # 400, NOT 404, and the distinction is the whole point: a 404 from this
        # route is consumed downstream as "not in the registry" -> byo_required ->
        # commercial. A selector that could never name a tenant is a client fault
        # and must not be able to produce a commercial verdict.
        logger.warning("byo_mode_selector_malformed: name_or_id=%r app=%r", name_or_id, app)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "malformed_tenant_selector",
                "remedy": (
                    "the selector must be a tenant UUID or a name matching ^[a-z0-9][a-z0-9-]*$"
                ),
            },
        )
    tenant = lookup.tenant
    if tenant is None:
        # This 404 is the silent-bypass signal, so it must not be silent. The caller
        # fails open on any non-200 (see the docstring above), so an unresolvable
        # tenant here means byo_required went UNENFORCED for that run — and until
        # this line existed, nothing anywhere recorded that it had. Measured on the
        # deployed pods: 34 of these in one 90-second burst, 0 log lines.
        # WARNING, not ERROR: this route is auth-exempt, so the severity is
        # caller-drivable, and the fleet runs log-level health checks on ERROR.
        logger.warning("byo_mode_tenant_unresolved: name_or_id=%r app=%r", name_or_id, app)
        # A DICT detail, not the old bare "Tenant not found" string. Three
        # different conditions can put a 404 on this path (absent tenant,
        # previously a malformed selector, and an unmatched route) and billing
        # branches on it to decide invoiceability, so the tenant's absence needs a
        # literal a generated client can match.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "tenant_not_found_in_registry", "tenant_selector": name_or_id},
        )
    byo_mode = tenant.byo_mode
    resolution = RESOLUTION_ROW
    if app:
        posture = db_manager._lookup_tenant_posture(str(tenant.id), app_key=app)
        byo_mode, resolution = posture.byo_mode, posture.resolution
        # `missing_row` is impossible here — the tenant row was just read — so the
        # only unresolved value this can carry is `fault`. Branching on the
        # constant rather than on `!= RESOLUTION_ROW` keeps that true if a future
        # path can return `missing_row` legitimately.
        if resolution == RESOLUTION_FAULT and on_unresolved != "fail_closed":
            # ERROR, not WARNING: unlike the 404s above, a caller cannot drive
            # this — it means this service could not read its own database twice.
            logger.error(
                "byo_mode_unresolved: tenant_id=%s app=%r — refusing to answer 200 with a "
                "fail-closed guess; a fabricated byo_required is read downstream as a "
                "durable COMMERCIAL verdict and frozen into an invoice",
                tenant.id,
                app,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "byo_mode_unresolved",
                    "tenant_id": str(tenant.id),
                    "app": app,
                    "remedy": "retry; this is not a verdict about the tenant",
                },
            )
    return {
        "tenant_id": str(tenant.id),
        "name": tenant.name,
        "byo_mode": byo_mode,
        "app": app,
        "resolution": resolution,
    }


@app.get(
    "/tenants/{name_or_id}/apps/{app_key}/byo-mode",
    response_model=TenantOfferingRecord,
    operation_id="get_tenant_app_byo_mode",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
async def get_tenant_app_byo_mode(
    name_or_id: str,
    app_key: str,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """The per-product BYO override for ONE ``(tenant, app)``, if there is one.

    Completes the per-app set. PUT and DELETE on this exact path have existed
    since posture rows were introduced; the matching READ did not, so a caller
    wanting one product had to fetch ``/tenants/{n}/offerings`` and filter — an
    unbounded response to answer a bounded question.

    ⚠️ THIS IS NOT THE EFFECTIVE POSTURE. It reports whether an OVERRIDE ROW
    exists and what is in it. A 404 here means "no override", after which the
    product resolves to the tenant-wide ``Tenant.byo_mode``. For "what applies to
    this product right now", call ``GET /tenants/{n}/byo-mode?app=<key>``, which
    performs that fallback. Treating a 404 from this route as ``local_allowed``
    is exactly the confusion that would let a byo_required tenant reach the
    platform LLM — the same failure the tokenless S2S lookup exists to prevent.

    ``offering_code: null`` means the provenance is UNKNOWN, never "no offering";
    ``offering_provenance`` states that explicitly so it cannot be misread.

    AUTHORIZATION mirrors ``list_tenant_offerings``: a non-admin may read only
    its OWN tenant, and a foreign tenant returns 404 rather than 403, because a
    403 confirms the tenant exists.

    Unlike the tokenless GET on ``/tenants/{n}/byo-mode``, this route is NOT in
    ``_BYO_MODE_OPEN_ROUTES`` — and it MUST NOT BE. That exemption skips the auth
    middleware entirely, so ``request.state.user`` is never populated; the
    dependency above would then refuse every caller, including a valid
    license-admin. Exempting this route does not open it, it breaks it.
    """
    tenant = await _read_tenant_for_caller(request, user, name_or_id)
    row = await asyncio.to_thread(db_manager.get_tenant_app_byo_mode, str(tenant.id), app_key)
    if row is None:
        # Deliberately one status for two causes. Distinguishing "no such app"
        # from "no override" would let an unprivileged caller enumerate the app
        # catalogue through this route, and the caller's next action is the same
        # either way: fall back to the tenant-wide posture.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No per-product BYO override for this tenant and app",
        )
    return row


@app.put(
    "/tenants/{name_or_id}/apps/{app_key}/byo-mode",
    response_model=TenantAppByoModeResponse,
    operation_id="set_tenant_app_byo_mode",
    tags=["tenants"],
    responses={**COMMON_ERROR_RESPONSES, **APP_SETTING_403_RESPONSE},
)
def set_tenant_app_byo_mode(
    name_or_id: str,
    app_key: str,
    body: TenantByoModeUpdate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Set a PER-PRODUCT BYO posture override for ``(tenant, app)``.

    Requires 'license-admin' or 'license-manager' AND the app: the tenant's owner
    must issue it or have brought it in with its own bundle; a grantee must issue it.
    Overrides the tenant-wide ``byo_mode`` for this
    product only, so a tenant can be 'byo_required' for product A but
    'platform_only' for product B. Remove with the DELETE variant to revert to
    the tenant-wide value.
    """
    _sel = str(_resolve_tenant_for_app_setting(user, name_or_id, app_key, "tenant_app_byo_mode").id)
    if body.byo_mode not in ("local_allowed", "byo_required", "platform_only"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="byo_mode must be 'local_allowed', 'byo_required', or 'platform_only'",
        )
    result = db_manager.set_tenant_app_byo_mode(_sel, app_key, body.byo_mode)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant or app not found")
    logger.info(
        f"Tenant-app byo_mode set: tenant={result['tenant_id']} ({result['tenant_name']}) "
        f"app={app_key} -> {body.byo_mode}"
    )
    # Plain dict, NOT JSONResponse — see set_tenant_byo_mode.
    return result


@app.delete(
    "/tenants/{name_or_id}/apps/{app_key}/byo-mode",
    response_model=TenantAppByoModeClearResponse,
    operation_id="clear_tenant_app_byo_mode",
    tags=["tenants"],
    responses={**COMMON_ERROR_RESPONSES, **APP_SETTING_403_RESPONSE},
)
def clear_tenant_app_byo_mode(
    name_or_id: str,
    app_key: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Remove a per-product BYO posture override, reverting ``(tenant, app)`` to
    the tenant-wide ``byo_mode``. Requires 'license-admin' or 'license-manager'
    AND the app (the tenant's owner must issue it or have brought it in with its own
    bundle; a grantee must issue it). Idempotent."""
    _sel = str(
        _resolve_tenant_for_app_setting(user, name_or_id, app_key, "tenant_app_byo_mode_clear").id
    )
    cleared = db_manager.clear_tenant_app_byo_mode(_sel, app_key)
    logger.info(f"Tenant-app byo_mode clear: tenant={name_or_id} app={app_key} cleared={cleared}")
    # Plain dict, NOT JSONResponse — see set_tenant_byo_mode.
    return {"tenant": name_or_id, "app_key": app_key, "cleared": cleared}


_OFFERING_USAGE_FIELDS = (
    "markup_pct",
    "surcharge_input_per_1m_usd",
    "surcharge_output_per_1m_usd",
    "surcharge_cache_creation_per_1m_usd",
    "surcharge_cache_read_per_1m_usd",
    "surcharge_per_request_usd",
)


class OfferingCreate(BaseModel):
    """Create/upsert a per-product offering (a reusable usage-policy template).

    Carries both axes: the USAGE policy (BYO ``byo_mode`` posture + ``markup_pct``
    and absolute surcharges, mirroring billing's ``markup_rules``) and the ACCESS
    policy (a fixed ``access_price_usd`` + ``access_cadence``). Provisioning it onto
    a tenant materializes the posture row, the tenant's billing markup rule, and —
    when priced or when it bundles an included-usage allowance — the tenant's
    billing access charge (carrying the allowance as ``included_cost_usd``).
    """

    app_key: str = Field(..., description="Product key (App.key), e.g. 'w2v-image'.")
    code: str = Field(
        ...,
        max_length=64,
        description="Offering code, unique per product (e.g. 'free-high-markup').",
    )
    name: str = Field(..., max_length=255)
    byo_mode: str = Field(..., description="'local_allowed', 'byo_required', or 'platform_only'")
    markup_pct: float | None = Field(
        None, ge=0, description="Relative markup percent on this product's BYO usage."
    )
    # ge=0 on every one, matching markup_pct above and the two ACCESS money fields
    # below. These five are copied verbatim into billing's markup rule (see
    # _OFFERING_USAGE_FIELDS and provision_tenant_offering), and
    # billing's services.markup.compute_billed_usd ADDS them into the billed
    # total — so a negative value is a silent discount, and with enough tokens a
    # negative invoice line. billing_service #78 bounds the identical five fields
    # on MarkupRuleCreate; without the same bound here an offering saves cleanly
    # and only fails at PROVISION time, as an opaque "502 Billing rejected the
    # markup-rule write (HTTP 422)" two services away from the field that caused
    # it. A discount is expressed as a lower markup_pct or an access-charge
    # allowance, never as negative money.
    #
    # ge (not gt): 0 is an explicit "charge nothing on this dimension", which
    # billing also accepts.
    surcharge_input_per_1m_usd: float | None = Field(default=None, ge=0)
    surcharge_output_per_1m_usd: float | None = Field(default=None, ge=0)
    surcharge_cache_creation_per_1m_usd: float | None = Field(default=None, ge=0)
    surcharge_cache_read_per_1m_usd: float | None = Field(default=None, ge=0)
    surcharge_per_request_usd: float | None = Field(default=None, ge=0)
    # ACCESS half of the offering: a fixed per-product access fee. NULL = free
    # access. Provisioned onto a tenant as a billing tenant_access_charge (S2S).
    access_price_usd: float | None = Field(
        None, ge=0, description="Fixed access fee for this product; NULL = free access."
    )
    access_cadence: Literal["monthly", "one_time"] = Field(
        "monthly", description="'monthly' (recurring) or 'one_time'."
    )
    # ALLOWANCE half of the ACCESS axis: a monthly USD usage credit bundled into
    # the grant. Provisioned as billing tenant_access_charge.included_cost_usd,
    # which offsets the tenant's incurred usage before markup. NULL = no credit.
    included_allowance_usd: float | None = Field(
        None, ge=0, description="Monthly included-usage credit (USD); NULL = none."
    )
    notes: str | None = None


class OfferingResponse(BaseModel):
    """One offering, exactly as ``models.Offering.to_dict`` emits it.

    Every field below is ALWAYS present in that dict — the nullable ones are
    ``null``, never absent — so they are required-and-nullable rather than
    optional. A generated client can therefore read ``.markup_pct`` without
    an existence check.
    """

    id: str = Field(..., description="Offering id (UUID).")
    app_id: str = Field(..., description="App id (UUID) this offering belongs to.")
    app_key: str | None = Field(
        ..., description="Product key (App.key); null only if the app relation is unloaded."
    )
    code: str = Field(..., description="Offering code, unique per product.")
    name: str = Field(..., description="Human-readable offering name.")
    byo_mode: str = Field(..., description="'local_allowed', 'byo_required', or 'platform_only'.")
    # USAGE half — copied verbatim into billing's markup rule at provision time.
    markup_pct: float | None = Field(..., description="Relative markup percent; null = none.")
    surcharge_input_per_1m_usd: float | None = Field(..., description="USD per 1M input tokens.")
    surcharge_output_per_1m_usd: float | None = Field(..., description="USD per 1M output tokens.")
    surcharge_cache_creation_per_1m_usd: float | None = Field(
        ..., description="USD per 1M cache-creation tokens."
    )
    surcharge_cache_read_per_1m_usd: float | None = Field(
        ..., description="USD per 1M cache-read tokens."
    )
    surcharge_per_request_usd: float | None = Field(..., description="USD per request.")
    # ACCESS half.
    access_price_usd: float | None = Field(..., description="Fixed access fee; null = free access.")
    access_cadence: str = Field(..., description="'monthly' or 'one_time'.")
    included_allowance_usd: float | None = Field(
        ..., description="Monthly included-usage credit (USD); null = none."
    )
    notes: str | None = Field(..., description="Free-form operator note.")
    created_at: str | None = Field(..., description="ISO-8601 creation timestamp.")
    updated_at: str | None = Field(..., description="ISO-8601 last-update timestamp.")


class OfferingListResponse(BaseModel):
    """The ``GET /offerings`` envelope."""

    offerings: list[OfferingResponse] = Field(
        ..., description="Offerings, oldest first; filtered to one product when ``?app=`` is given."
    )


class OfferingDeleteResponse(BaseModel):
    """Result of deleting an offering. Idempotent, so ``deleted`` may be false."""

    app_key: str = Field(..., description="Product key exactly as it was addressed.")
    code: str = Field(..., description="Offering code exactly as it was addressed.")
    deleted: bool = Field(..., description="True if a row was removed; false if none existed.")


@app.post(
    "/offerings",
    response_model=OfferingResponse,
    operation_id="create_offering",
    tags=["offerings"],
    responses={**COMMON_ERROR_RESPONSES, **APP_ADMIN_403_RESPONSE},
)
def create_offering(
    body: OfferingCreate,
    user: User = Depends(require_role("license-admin")),
):
    """Create or update a per-product offering (usage-policy template).

    Requires 'license-admin' AND that the caller's owner issues the app (403
    ``app_not_administrable_by_caller``; cross_owner bypasses, audited). An
    unknown app stays 404. Idempotent per ``(app_key, code)``.
    """
    app_row = db_manager.get_app_by_key(body.app_key)
    if app_row is not None:
        _authorize_app_admin(user, app_row.id, "offering_upsert")
    usage = {f: getattr(body, f) for f in _OFFERING_USAGE_FIELDS if getattr(body, f) is not None}

    def _upsert() -> dict[str, Any] | None:
        return db_manager.create_offering(
            body.app_key,
            body.code,
            body.name,
            body.byo_mode,
            usage=usage,
            access_price_usd=body.access_price_usd,
            access_cadence=body.access_cadence,
            included_allowance_usd=body.included_allowance_usd,
            notes=body.notes,
        )

    try:
        result = _upsert()
    # ClientSafeError, NOT ValueError: the DAL authored this message itself from
    # `body` (an invalid byo_mode / access_cadence), so echoing it is the caller's
    # actionable signal. A bare `except ValueError` would also catch whatever
    # psycopg/SQLAlchemy/pydantic raises inside create_offering, and THOSE
    # stringify to the failing statement plus its bound parameters. Any other
    # ValueError now falls through to the 500 handler with a generic body.
    except ClientSafeError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except IntegrityError:
        # Lost the read-then-insert race on ``ux_offering_app_code`` (models.py):
        # the DAL SELECTs ``(app_id, code)`` and INSERTs when absent, with no lock
        # between the two, so a concurrent POST for the same pair can commit in
        # the gap. Reachable today across the two replicas; routine once handlers
        # run in the threadpool.
        #
        # RETRY ONCE rather than return-the-existing-row: this route is an UPSERT
        # ("Idempotent per (app_key, code)"), so the caller's field values are
        # meant to land. On the retry the winner's row exists, the DAL takes its
        # UPDATE branch, and the caller's values are applied — which is exactly
        # what would have happened had the two requests not overlapped. Returning
        # the winner's row instead would silently discard this caller's write.
        logger.info(
            "Offering upsert lost an insert race for app=%s code=%s; retrying as an update",
            body.app_key,
            body.code,
        )
        try:
            result = _upsert()
        except IntegrityError as e:
            # Two consecutive violations are not the benign create/create race —
            # do not keep retrying, and do not 500. 409 tells the caller to retry.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Concurrent modification of offering '{body.app_key}/{body.code}'; "
                    "retry the request"
                ),
            ) from e
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"App not found: {body.app_key}"
        )
    logger.info(f"Offering upserted: app={body.app_key} code={body.code} byo={body.byo_mode}")
    # Plain dict, NOT JSONResponse: single 200 arm, no custom header. A returned
    # Response makes FastAPI skip `response_model`, so OfferingResponse would
    # document a shape nothing checks.
    return result


@app.get(
    "/offerings",
    response_model=OfferingListResponse,
    operation_id="list_offerings",
    tags=["offerings"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_offerings(
    app: str | None = Query(None, description="Filter to one product (App.key)."),
    user: User = Depends(require_role("license-admin")),
):
    """List offerings, optionally filtered to one product. Requires 'license-admin'."""
    return {"offerings": db_manager.list_offerings(app_key=app)}


@app.get(
    "/offerings/{app_key}/{code}",
    response_model=OfferingResponse,
    operation_id="get_offering",
    tags=["offerings"],
    responses=COMMON_ERROR_RESPONSES,
)
def get_offering(
    app_key: str,
    code: str,
    user: User = Depends(require_role("license-admin")),
):
    """Get a single offering by ``(app_key, code)``. Requires 'license-admin'."""
    result = db_manager.get_offering(app_key, code)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offering not found")
    return result


@app.delete(
    "/offerings/{app_key}/{code}",
    response_model=OfferingDeleteResponse,
    operation_id="delete_offering",
    tags=["offerings"],
    responses={**COMMON_ERROR_RESPONSES, **APP_ADMIN_403_RESPONSE},
)
def delete_offering(
    app_key: str,
    code: str,
    user: User = Depends(require_role("license-admin")),
):
    """Delete an offering by ``(app_key, code)``. Idempotent.

    Requires 'license-admin' AND that the caller's owner issues the app (403
    ``app_not_administrable_by_caller``; cross_owner bypasses, audited). An
    unknown app has no offering, so it answers ``deleted: false`` as before.
    """
    app_row = db_manager.get_app_by_key(app_key)
    if app_row is not None:
        _authorize_app_admin(user, app_row.id, "offering_delete")
    deleted = db_manager.delete_offering(app_key, code)
    logger.info(f"Offering delete: app={app_key} code={code} deleted={deleted}")
    # Plain dict, NOT JSONResponse — see create_offering.
    return {"app_key": app_key, "code": code, "deleted": deleted}


async def _provision_offering_markup(
    tenant_id: str, service_key: str, usage: dict[str, Any]
) -> int | None:
    """S2S: upsert the tenant's billing markup rule for one product.

    Mirrors ``_provision_metering_config``'s SA-token pattern. Posts to
    billing_service (``METERING_SERVICE_URL``) ``/api/config/markup-rules``.

    Returns the HTTP status billing answered with — 2xx means accepted — or
    ``None`` when billing was never reached at all (``METERING_SERVICE_URL``
    unset, or a transport error on both attempts). A status rather than a bool
    because the caller turns this into a 502 for a human who clicked "Provision"
    in fr, and "billing refused this" and "billing did not answer" need different
    remedies. Use :func:`_billing_write_succeeded` to test it.

    **Authorization.** ``POST /api/config/markup-rules`` is gated in
    billing_service by ``require_pricing_writer``, which admits the realm roles
    ``license-admin`` and ``pricing-writer``. The licences service account holds
    neither today, so this call 403s on every attempt until the ``pricing-writer``
    grant is made in Keycloak — that is the first thing to check when a provision
    fails. This paragraph is not hand-maintained prose: the dependency name and
    the role set are vendored from billing's own source into
    ``tests/support/billing_contract.json`` and asserted against this docstring
    by ``tests/unit/test_provision_docstrings.py``.

    Only ``_RETRYABLE_STATUSES`` get a second attempt; a 403 is answered once.
    """
    if not METERING_SERVICE_URL:
        logger.error("METERING_SERVICE_URL unset; cannot provision markup rule")
        return None

    import httpx

    from service_auth import get_service_token_provider

    token = await get_service_token_provider().get_token()
    headers: dict[str, str] = {"X-Tenant-Id": tenant_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("Service-account token unavailable; markup provision will likely 401/403")
    body = {"tenant_id": tenant_id, "service_key": service_key, **usage}

    # The LAST status billing actually answered with. A transport error on a
    # later attempt does not erase it: "billing said 503 then the socket died"
    # is more useful to the operator than "never reached billing".
    last_status: int | None = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(2):
            try:
                resp = await client.post(
                    f"{METERING_SERVICE_URL}/api/config/markup-rules",
                    json=body,
                    headers=headers,
                )
                last_status = resp.status_code
                if resp.status_code < 300:
                    return last_status
                if not _is_retryable_status(resp.status_code):
                    logger.error(
                        "markup-rule provision REFUSED by billing: HTTP %s (not retryable) "
                        "— check the licences SA's realm roles",
                        resp.status_code,
                    )
                    return last_status
                logger.warning(
                    "markup-rule provision returned %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            except httpx.HTTPError as exc:
                logger.warning("markup-rule provision transport error (attempt %d)", attempt)
                # A DETERMINISTIC transport fault is a verdict, exactly like a 403.
                # `UnsupportedProtocol` is raised BEFORE any I/O when the URL has
                # no scheme, so a retry re-raises identically a wasted second
                # later. The empty-string guard above catches METERING_SERVICE_URL
                # being UNSET; it does NOT catch it being SET WRONG
                # (`billing.licenses.svc:8080`, the ordinary gitops typo), which
                # is truthy and sails straight past it.
                if not _is_retryable_transport_error(exc):
                    return last_status
            if attempt == 0:
                await asyncio.sleep(1)
    return last_status


async def _provision_access_charge(
    tenant_id: str,
    service_key: str,
    amount_usd: float,
    cadence: str,
    included_cost_usd: float | None = None,
) -> int | None:
    """S2S: upsert the tenant's billing ACCESS charge for one product.

    Mirrors :func:`_provision_offering_markup` exactly (same SA-token pattern,
    same return contract). Posts to billing_service (``METERING_SERVICE_URL``)
    ``/api/config/access-charges``. Returns billing's HTTP status, or ``None``
    when billing was never reached.

    **Authorization.** ``POST /api/config/access-charges`` is gated in
    billing_service by ``require_pricing_writer``, admitting the realm roles
    ``license-admin`` and ``pricing-writer`` — the same gate as the markup write,
    and the licences service account holds neither until the ``pricing-writer``
    Keycloak grant lands. Derived from billing's source and asserted by
    ``tests/unit/test_provision_docstrings.py``; do not hand-edit the role names.

    Only ``_RETRYABLE_STATUSES`` get a second attempt.
    """
    if not METERING_SERVICE_URL:
        logger.error("METERING_SERVICE_URL unset; cannot provision access charge")
        return None

    import httpx

    from service_auth import get_service_token_provider

    token = await get_service_token_provider().get_token()
    headers: dict[str, str] = {"X-Tenant-Id": tenant_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning(
            "Service-account token unavailable; access-charge provision will likely 401/403"
        )
    body = {
        "tenant_id": tenant_id,
        "service_key": service_key,
        "amount_usd": amount_usd,
        "cadence": cadence,
    }
    if included_cost_usd is not None:
        body["included_cost_usd"] = included_cost_usd

    last_status: int | None = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(2):
            try:
                resp = await client.post(
                    f"{METERING_SERVICE_URL}/api/config/access-charges",
                    json=body,
                    headers=headers,
                )
                last_status = resp.status_code
                if resp.status_code < 300:
                    return last_status
                if not _is_retryable_status(resp.status_code):
                    logger.error(
                        "access-charge provision REFUSED by billing: HTTP %s (not retryable) "
                        "— check the licences SA's realm roles",
                        resp.status_code,
                    )
                    return last_status
                logger.warning(
                    "access-charge provision returned %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            except httpx.HTTPError as exc:
                logger.warning("access-charge provision transport error (attempt %d)", attempt)
                # A DETERMINISTIC transport fault is a verdict, exactly like a 403.
                # `UnsupportedProtocol` is raised BEFORE any I/O when the URL has
                # no scheme, so a retry re-raises identically a wasted second
                # later. The empty-string guard above catches METERING_SERVICE_URL
                # being UNSET; it does NOT catch it being SET WRONG
                # (`billing.licenses.svc:8080`, the ordinary gitops typo), which
                # is truthy and sails straight past it.
                if not _is_retryable_transport_error(exc):
                    return last_status
            if attempt == 0:
                await asyncio.sleep(1)
    return last_status


class TenantOfferingProvisionResponse(BaseModel):
    """What an offering provision actually materialized.

    The two ``*_provisioned`` flags are the honest report, not a success echo:
    each is false when the offering carried nothing to write on that axis (no
    markup/surcharge, no price and no allowance). A failed billing write does not
    reach this shape at all — it raises 502.
    """

    tenant_id: str = Field(..., description="Canonical tenant id (UUIDv7).")
    tenant_name: str = Field(..., description="Tenant name.")
    app_key: str = Field(..., description="Product key (App.key).")
    code: str = Field(..., description="Offering code.")
    byo_mode: str = Field(..., description="The posture written for this (tenant, product).")
    markup_rule_provisioned: bool = Field(
        ...,
        description="True if billing accepted a markup rule; false if the offering carried none.",
    )
    access_charge_provisioned: bool = Field(
        ...,
        description=(
            "True if billing accepted an access charge; false if the offering was neither "
            "priced nor bundling an allowance."
        ),
    )


class TenantOfferingUnprovisionResponse(BaseModel):
    """Result of unprovisioning an offering from a tenant. Idempotent."""

    tenant: str = Field(..., description="The tenant name or id exactly as it was addressed.")
    app_key: str = Field(..., description="Product key (App.key).")
    code: str = Field(..., description="Offering code exactly as it was addressed.")
    posture_cleared: bool = Field(
        ..., description="True if a per-product posture override was deleted."
    )
    note: str = Field(
        ...,
        description=(
            "Fixed disclosure that the billing markup rule survives this call — "
            "billing_service exposes no markup-rule delete."
        ),
    )


@app.post(
    "/tenants/{name_or_id}/offerings/{app_key}/{code}",
    response_model=TenantOfferingProvisionResponse,
    operation_id="provision_tenant_offering",
    tags=["offerings"],
    responses={**COMMON_ERROR_RESPONSES, **APP_SETTING_403_RESPONSE},
)
async def provision_tenant_offering(
    name_or_id: str,
    app_key: str,
    code: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Provision an offering onto a tenant (materialize its usage policy).

    Requires 'license-admin' or 'license-manager' AND the app: the tenant's owner
    must issue it or have brought it in with its own bundle; a grantee must issue it.
    Writes up to three stores: (1) the per-(tenant,
    product) posture override (``tenant_app_byo_mode``, local); (2) the tenant's
    billing markup rule (S2S) when the offering carries a markup/surcharge; and
    (3) the tenant's billing access charge (S2S) when the offering is priced or
    bundles an included-usage allowance.
    **Rollback is PARTIAL, and only the local write is genuinely undone.** If a
    billing write fails, the posture override is restored to the value it had
    before this call (or cleared when there was none) and a 502 naming the failed
    write and billing's status is returned.

    ⚠️ A failed provision CAN leave billing state behind. If the ACCESS-charge
    write fails AFTER the markup rule was already accepted, **that markup rule
    stays**: billing_service exposes no DELETE on ``/api/config/markup-rules``
    (GET and POST only), and billing resolves a markup by
    ``(tenant, service_key, …)`` independently of this service's posture — so the
    tenant is billed at that markup for a provision that reported failure. The
    502 detail says so explicitly rather than claiming a clean rollback.
    Re-provisioning overwrites all three and is the intended repair; see
    :func:`unprovision_tenant_offering` for the same asymmetry on the delete side.
    Idempotent.

    ⚠️ The two billing writes 403 on every call until the ``pricing-writer``
    realm role is granted to the licences service account in Keycloak; see
    :func:`_provision_offering_markup`.
    """
    _sel = str(
        (
            await asyncio.to_thread(
                _resolve_tenant_for_app_setting,
                user,
                name_or_id,
                app_key,
                "tenant_offering_provision",
            )
        ).id
    )
    # Blocking DB calls off the event loop — see ``introspect_token``. This handler
    # stays ``async def`` because it awaits the two billing hops below.
    offering = await asyncio.to_thread(db_manager.get_offering, app_key, code)
    if offering is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offering not found")

    posture = await asyncio.to_thread(
        db_manager.set_tenant_app_byo_mode,
        _sel,
        app_key,
        offering["byo_mode"],
        code,
    )
    if posture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant or app not found")
    tenant_id = posture["tenant_id"]
    # The override this call overwrote, if any — the value a rollback must put
    # back. ``set_tenant_app_byo_mode`` is an UPSERT, so "roll back" is not
    # "delete".
    prior_posture = posture["prior_byo_mode"]
    # Restored alongside it: without this a failed provision leaves ITS offering
    # code attributed to the tenant's own prior posture.
    prior_offering_code = posture["prior_offering_code"]

    usage = {f: offering[f] for f in _OFFERING_USAGE_FIELDS if offering.get(f) is not None}
    markup_provisioned = False
    if usage:
        markup_status = await _provision_offering_markup(tenant_id, app_key, usage)
        if not _billing_write_succeeded(markup_status):
            # Roll back the posture override so the tenant isn't half-provisioned.
            await asyncio.to_thread(
                _restore_tenant_app_posture,
                _sel,
                app_key,
                prior_posture,
                prior_offering_code,
            )
            # ``markup_status`` is billing's status code, not an exception and
            # not billing's response body — see tests/unit/test_no_exception_leak.py.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Billing rejected the markup-rule write (HTTP {markup_status}); "
                    "provisioning rolled back"
                    if markup_status is not None
                    else "Could not reach billing to write the markup rule; "
                    "provisioning rolled back"
                ),
            )
        markup_provisioned = True

    # ACCESS half: provision the tenant_access_charge when the offering carries a
    # price OR an included-usage allowance (a free-access tier can still bundle a
    # usage credit → amount 0, included N). On failure, roll back the local
    # posture (the markup write, if any, is idempotent and overwritten on
    # re-provision).
    access_provisioned = False
    if (
        offering.get("access_price_usd") is not None
        or offering.get("included_allowance_usd") is not None
    ):
        access_status = await _provision_access_charge(
            tenant_id,
            app_key,
            offering.get("access_price_usd") or 0,
            offering.get("access_cadence") or "monthly",
            included_cost_usd=offering.get("included_allowance_usd"),
        )
        if not _billing_write_succeeded(access_status):
            await asyncio.to_thread(
                _restore_tenant_app_posture,
                _sel,
                app_key,
                prior_posture,
                prior_offering_code,
            )
            # The markup rule written moments ago — if there was one — SURVIVES
            # this failure. billing has no markup-rule delete, so there is
            # nothing to un-do it with, and saying "provisioning rolled back"
            # while a live markup rule bills the tenant would be a lie in the
            # one place an operator is looking.
            residue = (
                " The markup rule written earlier in this request was NOT removed "
                "(billing has no markup-rule delete) — re-provision to overwrite it."
                if markup_provisioned
                else ""
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    (
                        f"Billing rejected the access-charge write (HTTP {access_status}); "
                        "the posture override was rolled back." + residue
                    )
                    if access_status is not None
                    else (
                        "Could not reach billing to write the access charge; "
                        "the posture override was rolled back." + residue
                    )
                ),
            )
        access_provisioned = True

    logger.info(
        f"Offering provisioned: tenant={tenant_id} app={app_key} code={code} "
        f"byo={offering['byo_mode']} markup_rule={markup_provisioned} "
        f"access_charge={access_provisioned}"
    )
    # Plain dict, NOT JSONResponse — see create_offering.
    return {
        "tenant_id": tenant_id,
        "tenant_name": posture["tenant_name"],
        "app_key": app_key,
        "code": code,
        "byo_mode": offering["byo_mode"],
        "markup_rule_provisioned": markup_provisioned,
        "access_charge_provisioned": access_provisioned,
    }


@app.delete(
    "/tenants/{name_or_id}/offerings/{app_key}/{code}",
    response_model=TenantOfferingUnprovisionResponse,
    operation_id="unprovision_tenant_offering",
    tags=["offerings"],
    responses={**COMMON_ERROR_RESPONSES, **APP_SETTING_403_RESPONSE},
)
def unprovision_tenant_offering(
    name_or_id: str,
    app_key: str,
    code: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Unprovision an offering from a tenant: clear the per-(tenant, product)
    posture override, reverting to the tenant-wide ``byo_mode``. Requires
    'license-admin' or 'license-manager' AND the app (the tenant's owner must issue
    it or have brought it in with its own bundle; a grantee must issue it). Idempotent.

    The billing markup rule is left in place — billing_service has no
    markup-rule delete endpoint today, and re-provisioning overwrites it. Markup
    only bills BYO usage, which the reverted posture governs.
    """
    _sel = str(
        _resolve_tenant_for_app_setting(user, name_or_id, app_key, "tenant_offering_unprovision").id
    )
    cleared = db_manager.clear_tenant_app_byo_mode(_sel, app_key)
    logger.info(
        f"Offering unprovisioned: tenant={name_or_id} app={app_key} code={code} "
        f"posture_cleared={cleared}"
    )
    # Plain dict, NOT JSONResponse — see create_offering.
    return {
        "tenant": name_or_id,
        "app_key": app_key,
        "code": code,
        "posture_cleared": cleared,
        "note": "billing markup rule left in place (no billing delete endpoint)",
    }


class TenantCreate(BaseModel):
    """Create a canonical tenant identity (name -> UUIDv7)."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        max_length=255,
        description=(
            "Lowercase alphanumeric + hyphen; unique WITHIN the calling app's tenants "
            "(e.g. 'acme-corp'). Another app may hold the same name."
        ),
    )
    external_ref: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description=(
            "The calling app's own stable id for this tenant (e.g. fr's tenant_slug). "
            "When sent, the call is idempotent on (owning app, external_ref): a retry "
            "returns the existing tenant (200)."
        ),
    )
    keycloak_realm: str | None = Field(
        None, max_length=255, description="Optional Keycloak realm this tenant authenticates in."
    )
    cost_centre: str | None = Field(
        None,
        max_length=64,
        description=(
            "Chargeback join key. One of: "
            + ", ".join(f"'{c}'" for c in COST_CENTRES)
            + " — or null. Constrained by ck_tenants_cost_centre (migration 034); "
            "an unlisted value is a 400, not a new centre. Deliberately left null "
            "until an apportionment basis exists, so leave it null unless you "
            "already know the answer — never guessed by this service. Settable "
            "afterwards via PATCH /tenants/{name_or_id}/cost-centre."
        ),
    )
    budget_currency: BudgetCurrency | None = Field(
        None,
        description=(
            "null (default) = token budgets; 'EUR' / 'USD' / 'CAD' = the tenant's budget "
            "is money in billing, so no token budget is ever pushed for it (migration "
            "043). Applied ONLY on INSERT; set it later with "
            "PUT /tenants/{name_or_id}/budget-currency."
        ),
    )


class TenantRegister(BaseModel):
    """Register a tenant whose UUID is already committed elsewhere (explicit-id upsert)."""

    name: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        max_length=255,
        description="Lowercase alphanumeric + hyphen; unique tenant name (e.g. 'acme-corp').",
    )
    keycloak_realm: str | None = Field(
        None, max_length=255, description="Optional Keycloak realm this tenant authenticates in."
    )
    cost_centre: str | None = Field(
        None,
        max_length=64,
        description=(
            "Chargeback join key. One of: "
            + ", ".join(f"'{c}'" for c in COST_CENTRES)
            + " — or null. Constrained by ck_tenants_cost_centre (migration 034). "
            "Deliberately left null until an apportionment basis exists. Applied "
            "ONLY on INSERT, exactly like `kind` and `byo_mode` — registering an "
            "EXISTING tenant id never overwrites its cost_centre; use "
            "PATCH /tenants/{name_or_id}/cost-centre to change one."
        ),
    )
    budget_currency: BudgetCurrency | None = Field(
        None,
        description=(
            "null (default) = token budgets; 'EUR' / 'USD' / 'CAD' = the tenant's budget "
            "is money in billing, so no token budget is ever pushed for it (migration "
            "043). Applied ONLY on INSERT; set it later with "
            "PUT /tenants/{name_or_id}/budget-currency."
        ),
    )
    kind: TenantKind | None = Field(
        None,
        description=(
            "Tenant classification (migration 027, ck_tenants_kind): customer | internal "
            "| service | test | unclassified. Omitted = the column default "
            "'unclassified' (fail closed: never silently counted as a customer or as "
            "internal). Applied ONLY on INSERT, like cost_centre and byo_mode — "
            "re-registering an EXISTING tenant id never overwrites its kind. "
            "kind='service' is born byo_mode='local_allowed' (migration 033)."
        ),
    )


def _reregister_existing(user: User, owner: str, existing: Tenant, body_name: str) -> JSONResponse:
    """``PUT /tenants/{id}`` on an id that is ALREADY registered.

    * the caller owns it -> 200, the row unchanged (idempotent re-register);
    * the caller holds an ACTIVE grant on it -> 200, the row unchanged. A grantee
      re-registers before every mint (commonpractice does, on the six shared
      platform tenants it was granted), so this must not be a conflict. It never
      renames and never changes the owner: a different body name is IGNORED and
      logged;
    * cross_owner (break-glass) -> 200 unchanged, audited;
    * otherwise 409 ``tenant_owned_by_other_app``.
    """
    if existing.owner_key != owner:
        if user.cross_owner:
            _audit_cross_owner(user, "tenant_register", existing)
        elif db_manager.owner_can_access_tenant(owner, str(existing.id)):
            if body_name != existing.name:
                logger.warning(
                    "tenant_register_by_grantee_name_ignored: tenant=%s owner=%s "
                    "grantee=%s stored_name=%r body_name=%r azp=%s",
                    existing.id,
                    existing.owner_key,
                    owner,
                    existing.name,
                    body_name,
                    user.azp,
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "tenant_owned_by_other_app",
                    "message": "this tenant id is registered to a different owning app",
                },
            )
    return JSONResponse(status_code=status.HTTP_200_OK, content=existing.to_dict())


@app.put(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="register_tenant",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {
            "model": TenantResponse,
            "description": (
                "Tenant already registered with this id, owned by the caller's app or "
                "granted to it (tenant_app_grant); returned UNCHANGED — a grantee's body "
                "name is ignored, never applied"
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "`unknown_api_client` — the calling client is not registered as an owning "
                "app (api_client); nothing was written."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing was written. `tenant_owned_by_other_app` (`detail.error`) — the "
                "id is already registered to a DIFFERENT owning app that has not granted "
                "the caller's app access. `tenant_name_taken` — "
                "the caller's own app already has a different tenant with this name "
                "(names are unique per owning app). A string `detail` — the id is a "
                "Keycloak principal id or another registration refusal."
            ),
        },
    },
)
def register_tenant(
    tenant_id: str,
    body: TenantRegister,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Register a tenant with a CALLER-SUPPLIED id, idempotently.

    ``POST /tenants`` mints its own UUIDv7 from the name, which is right when this
    service is the birthplace of the identity. It is wrong when the id already
    exists elsewhere and must match — a product that owns its own tenant table
    (commonpractice) would otherwise end up with two ids for one tenant, and the
    second one would mis-key billing (see ``auth/tenant.py`` on storing the wrong
    tenant key).

    This exposes the ``ensure_tenant`` primitive the ``provision_*`` scripts have
    always used directly, so a service can satisfy the license-mint guard
    ("a NEW license may only be minted for a REGISTERED tenant") over HTTP instead
    of needing DB access.

    Idempotent: re-registering the same id returns 200 with the existing row.
    409 if a DIFFERENT tenant of the caller's app already holds ``name`` (names are
    unique PER OWNING APP since migration 050), so a typo can never silently
    repoint an existing tenant.

    OWNERSHIP (migration 050): the tenant is created under the CALLER's owning
    app (api_client, from the verified token — never the body). An id already
    registered to another app is 200 UNCHANGED when that app granted the caller
    access (tenant_app_grant), else 409 ``tenant_owned_by_other_app``.
    """
    tid = validate_tenant_uuid(tenant_id, source="path tenant_id")
    owner = _require_known_owner(user)
    existing = db_manager.get_tenant(tid)
    if existing is not None:
        return _reregister_existing(user, owner, existing, body.name)
    try:
        tenant = db_manager.ensure_tenant(
            tid,
            name=body.name,
            keycloak_realm=body.keycloak_realm,
            kind=body.kind,
            cost_centre=body.cost_centre,
            budget_currency=body.budget_currency,
            owner_key=owner,
        )
    except TenantNameTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_name_taken",
                "name": body.name,
                "message": f"tenant name already in use by this app: {body.name!r}",
            },
        ) from exc
    # ClientSafeError, NOT ValueError: `ensure_tenant` authors this message itself
    # ("name X already belongs to tenant Y") from the caller's own body, and it is
    # the caller's only actionable signal. A bare `except ValueError` would also
    # catch a psycopg/SQLAlchemy error whose str() carries the failing statement
    # and its bound parameters straight into the 409 body.
    except ClientSafeError as exc:
        # Name owned by another tenant — a conflict the caller must resolve, not a 500.
        logger.warning(f"Tenant register conflict for {tid}: {exc}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if str(tenant.owner_key) != owner:
        # Lost a race to another app registering the SAME id between our read and
        # ensure_tenant's insert: ensure_tenant returned the winner's row.
        return _reregister_existing(user, owner, tenant, body.name)
    logger.info(
        "Tenant registered with explicit id: %s (%s) owner=%s kind=%s cost_centre=%s by %s",
        tenant.id,
        tenant.name,
        tenant.owner_key,
        tenant.kind,
        tenant.cost_centre,
        user.sub,
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=tenant.to_dict())


@app.post(
    "/tenants",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_tenant",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {
            "model": TenantResponse,
            "description": (
                "Existing tenant returned unchanged: this app already registered a LIVE "
                "tenant with the sent `external_ref` (idempotent retry; an archived tenant "
                "never matches)"
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing was created: `tenant_name_taken` (`detail.error`) — the calling "
                "app already has a LIVE tenant with this name. A name held by ANOTHER app, "
                "or only by an ARCHIVED tenant of this app, is not a conflict."
            ),
        },
    },
)
def create_tenant(
    body: TenantCreate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Create a canonical tenant under the CALLER's owning app.

    Requires 'license-admin' or 'license-manager'. The tenant registry is the foundation of the
    cluster-wide ``(UUIDv7 + name)`` identity standard; the provisioning service
    calls this as step one of post-sales onboarding.

    OWNERSHIP (migration 050). The tenant is owned by the caller's app (api_client,
    from the verified token — never the body). This route is NO LONGER idempotent
    by name: it used to return the existing tenant for a taken name, which handed
    fr commonpractice's tenant UUID when both had a customer called ``acme`` — a
    cross-tenant merge. Now:

    * ``external_ref`` sent and already registered by this app on a LIVE tenant ->
      200, that tenant (the retry-safe path; provisioning sends fr's ``tenant_slug``);
    * name taken by ANOTHER app -> 201, a NEW tenant (names are unique per owner);
    * name taken by a LIVE tenant of THIS app -> 409 ``tenant_name_taken``.

    ARCHIVED tenants never match (migration 053): names and external_refs are unique
    per owner among live tenants only. Re-registering an archived tenant's name, or
    re-provisioning its external_ref, is 201 with a NEW tenant; the archived one
    stays as it was, reachable by id.
    """
    owner = _require_known_owner(user)
    if body.external_ref is not None:
        prior = db_manager.get_tenant_by_external_ref(owner, body.external_ref)
        if prior is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=prior.to_dict())
    try:
        tenant = db_manager.create_tenant(
            name=body.name,
            keycloak_realm=body.keycloak_realm,
            cost_centre=body.cost_centre,
            budget_currency=body.budget_currency,
            owner_key=owner,
            external_ref=body.external_ref,
        )
    except IntegrityError as exc:
        # A unique index fired: either a concurrent create with the same
        # external_ref won (idempotent -> return it) or the name is taken within
        # this owner (409). Never another owner's row.
        if body.external_ref is not None:
            prior = db_manager.get_tenant_by_external_ref(owner, body.external_ref)
            if prior is not None:
                return JSONResponse(status_code=status.HTTP_200_OK, content=prior.to_dict())
        if db_manager.get_tenant_scoped(body.name, owner, allow_grant=False) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "tenant_name_taken",
                    "name": body.name,
                    "message": f"tenant name already in use by this app: {body.name!r}",
                },
            ) from exc
        raise
    logger.info(f"Tenant created: {tenant.id} ({tenant.name}) owner={tenant.owner_key}")
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=tenant.to_dict())


@app.patch(
    "/tenants/{name_or_id}",
    response_model=TenantResponse,
    operation_id="rename_tenant",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing was renamed: `tenant_name_taken` (`detail.error`) — another "
                "tenant already holds the requested name. `detail` also carries `name`. "
                "Returned for the pre-check and for a concurrent rename that won the "
                "unique index first."
            ),
        },
    },
)
async def rename_tenant(
    name_or_id: str,
    body: TenantRename,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Rename a tenant. Requires 'license-admin' or 'license-manager' on a tenant
    the caller OWNS (grantee: 403 ``tenant_owner_only``).

    This is the UPDATE that `PUT /tenants/{tenant_id}` is not: that route is
    `register_tenant`, an idempotent register-if-absent which returns an existing
    row UNCHANGED. There was no way to correct a tenant name through the API at
    all — a typo at provisioning time was permanent.

    Idempotent: renaming to the current name returns 200 unchanged rather than
    409ing against the tenant's own name.
    """
    _sel = str(
        (await asyncio.to_thread(_resolve_owned_tenant, user, name_or_id, "tenant_rename")).id
    )
    # ClientSafeError, NOT a wider catch: the DAL authored both messages from the
    # caller's own input. Before #258 neither was caught, so a taken name and an
    # invalid name both surfaced as a 500.
    try:
        tenant = await asyncio.to_thread(db_manager.rename_tenant, _sel, body.name)
    except TenantNameTakenError as exc:
        # Pre-check AND the unique-index race both land here (models.rename_tenant).
        # The detail is built from the request body, not the exception: this arm
        # echoes nothing it caught.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_name_taken",
                "name": body.name,
                "message": f"tenant name already in use: {body.name!r}",
            },
        ) from exc
    except ClientSafeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tenant not found: {name_or_id}")
    logger.info(f"Tenant {tenant.id} renamed to {tenant.name}")
    return TenantResponse(**tenant.to_dict())


@app.patch(
    "/tenants/{name_or_id}/archive",
    response_model=TenantResponse,
    operation_id="set_tenant_archived",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing was restored: `tenant_name_taken` / `tenant_external_ref_taken` "
                "(`detail.error`) — while this tenant was archived, a LIVE tenant of the "
                "same owning app took its name / external_ref (both are unique among live "
                "tenants only, migration 053). Rename the archived tenant first."
            ),
        },
    },
)
async def set_tenant_archived(
    name_or_id: str,
    body: TenantArchiveUpdate,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Retire a tenant, or restore one. Requires 'license-admin' or 'license-manager'
    on a tenant the caller OWNS (grantee: 403 ``tenant_owner_only``).

    The closest thing to a delete this API has, and deliberately not one. A
    tenant owns licences, tokens, budgets, workspaces and fr `user_roles` rows;
    cascading a delete through those would destroy billable history and the
    audit trail needed to undo the mistake.

    ⚠️ THIS STILL REVOKES NOTHING — but it is no longer INERT, and the two are
    different claims. No row is destroyed and no `revoked_at` is written:
    licences, tokens, budgets and history all survive exactly as before, which
    is what makes un-archiving a true restore. What CHANGED is that an archived
    tenant's licence no longer AUTHORIZES: `/license/introspect` answers
    `active: false, reason: "tenant_archived"` while `archived_at` is set, and
    answers `active: true` again the moment it is cleared. Refusal, not
    revocation.

    ⭐ THIS ENDPOINT'S DOCSTRING USED TO CALL THAT A KNOWN GAP — "a real
    offboarding flow (archive + revoke every live licence, in one auditable act)
    is owed and does not exist yet". This is the gap being closed, and it is
    closed by REFUSING rather than by revoking, deliberately: revoking would
    make archiving destructive and therefore not reversible, and it would need a
    second write across every licence. A read-time refusal keyed on one nullable
    column is reversible by definition.

    ⛔ WHERE IT IS REFUSED. THREE GATES, ALL REFUSALS, NONE OF THEM REVOCATIONS:

      1. `models._introspect_from_session` — an archived tenant's licence answers
         `active: false, reason: "tenant_archived"`. This is the authz boundary
         ext_authz and every on-behalf-of caller ask BEFORE doing chargeable work,
         which is why it is the one that stops spend.
      2. `POST /licenses` (`create_user_license`) — no NEW licence may be minted
         for an archived tenant.
      3. `POST /licenses/{license_id}/tokens` (`create_license_token`) — no NEW
         credential may be minted against an archived tenant's existing licence.
         ⭐ This was the hole: gates 1 and 2 shipped together and left it. A
         perpetual token minted after the retirement is refused at introspect
         today, but it OUTLIVES the archive — un-archive for any reason and every
         token issued in the interval silently becomes live. Gate 3 keeps the
         credential set a restored tenant wakes up with equal to the set it had
         when it was retired.

    ⛔ AND HERE IS WHAT ARCHIVING STILL CANNOT STOP. Saying this plainly is part
    of the contract, because all three gates share one precondition — the caller
    has to ASK this service something:

      * A cost event posted against a bare `tenant_id` with `license_id = NULL`.
        Re-measured 2026-09-12: `rmt-e2e`, archived 2026-09-03, accrued **461**
        cost events / **$1.60** in the nine days after, ALL of them `license_id =
        NULL`, posted by `inference-router` and `mlx-ingestion-backend` on a path
        that never calls this service. ⛔ NOTHING IN THIS REPO CAN REACH THAT, and
        no gate added here will change the number. Closing it means requiring a
        RESOLVED licence at the cost-event writer, which lives outside this repo.
      * An already-issued token, for exactly as long as anything accepts it
        WITHOUT introspecting. Archiving revokes nothing by design (see below), so
        a consumer that caches an introspect result, or trusts a token locally,
        keeps working until its cache expires. This service cannot bound that.
      * Any direct write to the database that bypasses these routes — the same
        blind spot `ensure_tenant`'s principal-id guard has against raw SQL.
      * Spend attributed to a DIFFERENT tenant id that the archived tenant's users
        still hold a licence on. Archiving is per-tenant-row, not per-person.

    ⚠️ So `TenantSpendOnArchivedRegistryRow` WILL NOT CLEAR when this merges, and
    that is not these gates failing. The alert's population (archived + billing
    rows) and this service's population (archived + presents a licence) are
    DISJOINT today.
    ⛔ Do NOT "improve" this by dropping the tenant's cost/billing rows instead:
    that converts an attribution fault into an UNMETERED one — the compute is
    still spent, it is merely no longer visible — which is strictly worse than
    the problem. Refuse upstream; never discard a cost event.

    ⚠️ ARCHIVING IS NOT A COMPLETE OFF-SWITCH, AND CLAIMING IT IS WOULD BE FALSE.
    This is the enforcement half of gitops #3795, which shipped the detector
    `TenantSpendOnArchivedRegistryRow` and filed this half as licenses#197. The
    `rmt-e2e` figure above has been re-measured twice and grew both times (405 /
    $1.6018 on 2026-09-11 → 461 / $1.60 on 2026-09-12) without any gate here
    failing — because those events never reach this service. ⚠️ Re-measure it;
    do not quote a remembered number.

    Pinned by `tests/unit/test_tenant_rename_and_archive.py::TestArchivingRevokesNothing`
    (stored state survives), `tests/unit/test_archived_tenant_gates_spend.py`
    (the licence stops authorizing),
    `tests/unit/test_mint_refuses_an_archived_tenant.py` (gate 2) and
    `tests/unit/test_archived_tenant_cannot_mint_a_token.py` (gate 3), so no half
    can drift silently.
    """
    _sel = str(
        (await asyncio.to_thread(_resolve_owned_tenant, user, name_or_id, "tenant_archive")).id
    )
    try:
        tenant = await asyncio.to_thread(db_manager.set_tenant_archived, _sel, body.archived)
    except TenantNameTakenError as exc:
        # Un-archive into a name a live tenant of the same owner now holds (053).
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_name_taken",
                "tenant_id": _sel,
                "message": (
                    "a live tenant of this app now holds this tenant's name; rename the "
                    "archived tenant before restoring it"
                ),
            },
        ) from exc
    except TenantExternalRefTakenError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "tenant_external_ref_taken",
                "tenant_id": _sel,
                "message": (
                    "a live tenant of this app now holds this tenant's external_ref; "
                    "the archived tenant cannot be restored beside it"
                ),
            },
        ) from exc
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tenant not found: {name_or_id}")
    logger.info(f"Tenant {tenant.id} ({tenant.name}) {'archived' if body.archived else 'restored'}")
    return TenantResponse(**tenant.to_dict())


class TenantGrantResponse(BaseModel):
    """An app's grant on another app's tenant (migration 050)."""

    tenant_id: str
    grantee_owner_key: str
    granted_by: str
    created_at: str | None = None
    revoked_at: str | None = None


def _grant_target(user: User, name_or_id: str, owner_key: str) -> Tenant:
    tenant = _resolve_owned_tenant(user, name_or_id, f"tenant_grant:{owner_key}")
    if not db_manager.owner_exists(owner_key):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "owner_not_found", "owner_key": owner_key},
        )
    return tenant


@app.put(
    "/tenants/{name_or_id}/grants/{owner_key}",
    response_model=TenantGrantResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="put_tenant_grant",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {"model": TenantGrantResponse, "description": "The grant already existed."},
        403: {
            "model": ErrorResponse,
            "description": (
                "`unknown_api_client` — the caller is not an owning app; "
                "`tenant_owner_only` — the caller holds only a grant on this tenant "
                "(grantees may not grant onward)."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "Tenant not found (or not the caller's), or `owner_not_found`.",
        },
        422: {
            "model": ErrorResponse,
            "description": "`grant_to_owner` — the tenant's owner needs no grant.",
        },
    },
)
def put_tenant_grant(
    name_or_id: str,
    owner_key: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Let ANOTHER app act on this tenant (migration 050). Owner (or break-glass) only.

    The grantee may then read the tenant BY ID and mint / revoke licences for the
    apps it issues. It may not rename, archive, change BYO mode, or grant onward.
    Idempotent: an existing active grant is returned with 200.
    """
    tenant = _grant_target(user, name_or_id, owner_key)
    if owner_key == tenant.owner_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "grant_to_owner",
                "message": "the tenant's owning app needs no grant on its own tenant",
            },
        )
    granted_by = f"{user.realm}/{user.azp}" if user.azp else str(user.sub)
    grant, created = db_manager.put_tenant_grant(str(tenant.id), owner_key, granted_by)
    logger.warning(
        "tenant_grant_put: tenant=%s owner=%s grantee=%s by=%s created=%s",
        tenant.id,
        tenant.owner_key,
        owner_key,
        granted_by,
        created,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=grant.to_dict(),
    )


@app.delete(
    "/tenants/{name_or_id}/grants/{owner_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_tenant_grant",
    tags=["tenants"],
    responses={
        **COMMON_ERROR_RESPONSES,
        403: {
            "model": ErrorResponse,
            "description": "`unknown_api_client` or `tenant_owner_only`.",
        },
        404: {
            "model": ErrorResponse,
            "description": "Tenant not found, `owner_not_found`, or `grant_not_found`.",
        },
    },
)
def delete_tenant_grant(
    name_or_id: str,
    owner_key: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """Revoke another app's grant on this tenant. Owner (or break-glass) only.

    Stamps ``revoked_at`` (history kept). Licences the grantee already minted are
    NOT revoked — revoke those explicitly; this only stops further issuance.
    """
    tenant = _grant_target(user, name_or_id, owner_key)
    active = [
        g for g in db_manager.list_tenant_grants(str(tenant.id)) if g.grantee_owner_key == owner_key
    ]
    if not active or not db_manager.revoke_tenant_grant(str(tenant.id), owner_key):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "grant_not_found", "owner_key": owner_key},
        )
    logger.warning(
        "tenant_grant_revoked: tenant=%s owner=%s grantee=%s by=%s/%s",
        tenant.id,
        tenant.owner_key,
        owner_key,
        user.realm,
        user.azp,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/tenants/{name_or_id}/grants",
    response_model=list[TenantGrantResponse],
    operation_id="list_tenant_grants",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_tenant_grants(
    name_or_id: str,
    user: User = Depends(require_any_role(*OWNER_WRITE_ROLES)),
):
    """The ACTIVE grants on a tenant. Owner (or break-glass) only."""
    tenant = _resolve_owned_tenant(user, name_or_id, "tenant_grant_list")
    return [g.to_dict() for g in db_manager.list_tenant_grants(str(tenant.id))]


@app.get(
    "/tenants",
    response_model=list[TenantResponse],
    operation_id="list_tenants",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
async def list_tenants(
    request: Request,
    include_archived: bool = Query(
        False,
        description=(
            "Include retired tenants. Off by default because this list is a "
            "pick-list and a retired tenant should not be pickable; the admin view "
            "needs it ON to un-archive one, otherwise archiving is a one-way door."
        ),
    ),
    include_service_actors: bool = Query(
        False,
        description=(
            "Include service-mesh auth principals (kind='service'). Off by default: "
            "migration 027 defines them as NOT tenants in the commercial sense and "
            "says they should be excluded from any tenant inventory. They are 33 of "
            "the 59 live rows. Turn ON for the admin completeness view and for "
            "provisioning a licence to a service principal."
        ),
    ),
    user: User = Depends(
        require_any_role("license-admin", "license-reader", TENANT_REGISTRY_READER_ROLE)
    ),
):
    """List tenants (newest first).

    Requires 'license-admin', 'tenant-registry-reader', or 'license-reader'.

    AUTHORIZATION (customer-roster leak closed 2026-07-27): this returned the
    WHOLE tenants table — i.e. every customer's name and id — to any
    ``license-reader``, including a token-scoped reader minted for a single
    tenant. A non-admin caller sees only its own tenant; ``license-admin`` keeps
    the full operator listing.

    ⭐ ROSTER READ WITHOUT ADMIN (added for billing-service): that fix left no
    way to ASK the registry a question about a tenant that is not your own, and
    the only role that could was ``license-admin`` — which can also create,
    rename and archive tenants. ``billing-service``'s unregistered-tenant gate
    needs exactly the question and none of the power: it observes a ``tenant_id``
    on a cost event and must decide whether that tenant exists in the licences
    registry (``billing_cost_events`` has no FK to ``tenants``, so an unknown
    tenant is otherwise undetectable — see ``models.py``). It held neither role,
    got a 403 here, and **shipped that gate turned OFF**.

    ``tenant-registry-reader`` is the least-privilege answer: it is admitted to
    the full listing, and to NOTHING else in this service — it is honoured by
    this one route and appears in no other gate, so it grants no write, no
    licence read, and no ``/internal/*`` surface. Deliberately NOT solved by
    adding billing to ``INTERNAL_SERVICE_ROLES``, which would have handed it the
    fleet-wide ``/internal/licenses*`` listings and the cross-tenant entitlement
    lookup as a side effect.
    """
    # Blocking DB calls off the event loop — see ``introspect_token``. This handler
    # stays ``async def`` because it awaits ``get_tenant_id``.
    # ⭐ OWNER SCOPE (migration 050). `tenant-registry-reader` (billing's
    # registry question) and the cross_owner break-glass client keep the full
    # roster. A `license-admin` client sees only the tenants its OWN app owns —
    # holding the role no longer implies cross-app reach. A license-admin whose
    # client is not in api_client falls through to the own-tenant view below.
    await asyncio.to_thread(_ensure_owner_resolved, user)
    if user.has_realm_role(TENANT_REGISTRY_READER_ROLE) or (
        user.has_realm_role("license-admin") and user.cross_owner
    ):
        return [
            t.to_dict()
            for t in await asyncio.to_thread(
                db_manager.list_tenants, include_archived, include_service_actors
            )
        ]
    if user.has_realm_role("license-admin") and user.owner_key is not None:
        return [
            t.to_dict()
            for t in await asyncio.to_thread(
                db_manager.list_tenants,
                include_archived,
                include_service_actors,
                user.owner_key,
            )
        ]

    caller_tenant = await get_tenant_id(request)
    own = await asyncio.to_thread(db_manager.get_tenant, caller_tenant)
    return [own.to_dict()] if own is not None else []


@app.get(
    "/tenants/{name_or_id}",
    response_model=TenantResponse,
    operation_id="get_tenant",
    tags=["tenants"],
    responses=COMMON_ERROR_RESPONSES,
)
async def get_tenant(
    name_or_id: str,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """Resolve a tenant by UUID or name. Requires 'license-admin' or 'license-reader'.

    AUTHORIZATION (cross-tenant read closed 2026-07-27): a non-admin caller may
    resolve only its OWN tenant. A foreign tenant returns 404, NOT 403 —
    a 403 would confirm that the id/name exists, which is itself the leak this
    closes (an attacker could enumerate the customer roster one guess at a
    time). Indistinguishable-from-absent is the point.
    """
    tenant = await _read_tenant_for_caller(request, user, name_or_id)
    return tenant.to_dict()


async def _read_tenant_for_caller(request: Request, user: User, name_or_id: str) -> Tenant:
    """The tenant-READ rule shared by ``get_tenant`` / ``list_tenant_offerings`` /
    ``get_tenant_app_byo_mode``.

    * an owned client holding ``license-admin`` (migration 050): names resolve only
      within its own app; an id resolves if its app owns the tenant or holds an
      active grant on it; anything else 404 (never 403 — existence is not leaked).
      cross_owner reads anything, audited.
    * everyone else (per-tenant tokens, a license-admin whose client is not in
      api_client): its OWN tenant only, 404 otherwise.
    """
    await asyncio.to_thread(_ensure_owner_resolved, user)
    if user.has_realm_role("license-admin") and (user.owner_key or user.cross_owner):
        return await asyncio.to_thread(_resolve_tenant_for_caller, user, name_or_id)
    caller_tenant = await get_tenant_id(request)
    try:
        UUID(str(name_or_id))
        tenant = await asyncio.to_thread(db_manager.get_tenant, name_or_id)
    except ValueError, TypeError, AttributeError:
        # A NAME: resolve only within the caller's own tenant row.
        own = await asyncio.to_thread(db_manager.get_tenant, str(caller_tenant))
        tenant = own if own is not None and own.name == name_or_id else None
    if tenant is None or str(tenant.id) != str(caller_tenant):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


@app.get(
    "/tenants/{name_or_id}/offerings",
    response_model=list[TenantOfferingRecord],
    operation_id="list_tenant_offerings",
    tags=["offerings"],
    responses=COMMON_ERROR_RESPONSES,
)
async def list_tenant_offerings(
    name_or_id: str,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """What this tenant has been provisioned, per product.

    Requires 'license-admin' or 'license-reader'. This service owns only HALF of
    what provisioning writes: the per-product posture row here, and — via S2S —
    the tenant's markup rule and access charge in billing_service. This route
    reports its own half completely; it is not the tenant's billing picture.

    ⚠️ ``offering_code: null`` does NOT mean "no offering", it means the
    provenance is unknown: set directly via the per-app byo-mode PUT, or written
    before migration 025 added the column. It could not be backfilled because the
    code had never been recorded anywhere. A caller rendering null as an empty
    cell asserts "nothing provisioned" about rows that may well have been.

    An empty list means the tenant carries NO per-product overrides — every
    product then resolves to the tenant-wide ``Tenant.byo_mode``, which is not
    reported here because it is not a per-product fact. Read it from the tenant.

    AUTHORIZATION mirrors :func:`get_tenant` exactly: a non-admin may read only
    its OWN tenant, and a foreign tenant returns 404 rather than 403 — a 403
    would confirm the tenant exists, which is the enumeration leak that shape
    closes. A tenant that does not exist is likewise 404, so an operator can tell
    "no such tenant" from "no overrides" ([] is the latter).
    """
    # Blocking DB calls off the event loop — see ``introspect_token``. This
    # handler stays ``async def`` because it awaits ``get_tenant_id`` below.
    tenant = await _read_tenant_for_caller(request, user, name_or_id)
    rows = await asyncio.to_thread(db_manager.list_tenant_offerings, str(tenant.id))
    if rows is None:
        # Only reachable if the tenant is deleted between the two lookups.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return rows


@app.get(
    "/apps",
    response_model=list[AppResponse],
    operation_id="list_apps",
    tags=["apps"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_apps(
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all apps

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        apps = db_manager.list_apps()
        return [AppResponse(**app.to_dict()) for app in apps]

    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to list apps", e, "Failed to list apps"
        ) from e


@app.get(
    "/apps/{app_id}",
    response_model=AppResponse,
    operation_id="get_app",
    tags=["apps"],
    responses=COMMON_ERROR_RESPONSES,
)
def get_app(
    app_id: str,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    Get app by ID

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        # Try by UUID first, then by key
        app = None
        try:
            app = db_manager.get_app(UUID(app_id))
        except ValueError:
            app = db_manager.get_app_by_key(app_id)

        if not app:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

        return AppResponse(**app.to_dict())

    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to get app", e, "Failed to get app"
        ) from e


@app.get(
    "/apps/{app_id}/plans",
    response_model=list[PlanResponse],
    operation_id="list_app_plans",
    tags=["apps"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_app_plans(
    app_id: str,
    include_revoked: bool = Query(
        False,
        description=(
            "Also return revoked (retired) plans. Default false: the listing is the "
            "catalogue of plans a new licence can be put on."
        ),
    ),
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all plans for a specific app

    Revoked plans are excluded unless ``include_revoked=true``.

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        # Get app first
        app = None
        try:
            app = db_manager.get_app(UUID(app_id))
        except ValueError:
            app = db_manager.get_app_by_key(app_id)

        if not app:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

        plans = db_manager.list_plans(app_id=UUID(str(app.id)), include_revoked=include_revoked)
        return [PlanResponse(**plan.to_dict()) for plan in plans]

    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list app plans",
            e,
            "Failed to list app plans",
        ) from e


@app.get(
    "/apps/{app_id}/licenses",
    response_model=list[LicenseResponse],
    operation_id="list_app_licenses",
    tags=["apps"],
    responses=COMMON_ERROR_RESPONSES,
)
async def list_app_licenses(
    app_id: str,
    request: Request,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all licenses for a specific app

    Requires 'license-admin' or 'license-reader' role.

    AUTHORIZATION (cross-tenant leak closed 2026-07-27): apps are a GLOBAL
    catalogue, so an app_id authorizes nothing about tenancy — this used to
    enumerate every tenant's licenses for the app to any ``license-reader``.
    Non-admin callers now see only their own tenant's licenses; a
    ``license-admin`` remains the cross-tenant operator surface (same contract
    as ``GET /licenses/admin/all``).
    """
    try:
        # Blocking DB calls off the event loop — see ``introspect_token``. This
        # handler stays ``async def`` because it awaits ``get_tenant_id``.
        # Get app first
        app = None
        try:
            app = await asyncio.to_thread(db_manager.get_app, UUID(app_id))
        except ValueError:
            app = await asyncio.to_thread(db_manager.get_app_by_key, app_id)

        if not app:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

        tenant_filter: str | None = None
        if not user.has_realm_role("license-admin"):
            tenant_filter = await get_tenant_id(request)

        licenses = await asyncio.to_thread(
            db_manager.list_licenses, app_id=UUID(str(app.id)), tenant_id=tenant_filter
        )
        return [_enrich_license_response(license) for license in licenses]

    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list app licenses",
            e,
            "Failed to list app licenses",
        ) from e


# ============= Plan Endpoints =============


@app.post(
    "/plans",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_plan",
    tags=["plans"],
    responses={
        **COMMON_ERROR_RESPONSES,
        **APP_ADMIN_403_RESPONSE,
        409: {
            "model": ErrorResponse,
            "description": "Plan code already exists for this app/region",
        },
    },
)
def create_plan(plan_data: PlanCreate, user: User = Depends(require_role("license-admin"))):
    """
    Create a new plan

    Requires 'license-admin' role AND that the caller's owner issues the app
    (403 ``app_not_administrable_by_caller``; cross_owner bypasses, audited).
    """
    if not db_manager.get_app(plan_data.app_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"App {plan_data.app_id} not found",
        )
    _authorize_app_admin(user, plan_data.app_id, "plan_create")

    if plan_data.region_id and not db_manager.get_region(plan_data.region_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Region {plan_data.region_id} not found",
        )

    try:
        plan = db_manager.create_plan(
            app_id=plan_data.app_id,
            code=plan_data.code,
            name=plan_data.name,
            description=plan_data.description,
            region_id=plan_data.region_id,
            duration_days=plan_data.duration_days,
            rate_limit_rpm=plan_data.rate_limit_rpm,
            rate_limit_burst=plan_data.rate_limit_burst,
            quota_monthly=plan_data.quota_monthly,
            token_budget_monthly=plan_data.token_budget_monthly,
        )

        logger.info(f"Plan created: {plan.id} ({plan.code})")

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=PlanResponse(**plan.to_dict()).model_dump(),
        )

    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Plan with code '{plan_data.code}' already exists for this app/region",
        )
    # Top-level handler: spans DB + JSONResponse build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create plan",
            e,
            "Failed to create plan",
        ) from e


@app.patch(
    "/plans/{plan_id}",
    response_model=PlanResponse,
    operation_id="update_plan",
    tags=["plans"],
    responses={**COMMON_ERROR_RESPONSES, **APP_ADMIN_403_RESPONSE},
)
def update_plan(
    plan_id: str,
    plan_data: PlanUpdate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role("license-admin")),
):
    """
    Update an existing plan's mutable fields

    Requires 'license-admin' role AND that the caller's owner issues the plan's
    app (403 ``app_not_administrable_by_caller``; cross_owner bypasses, audited).

    A change to ``token_budget_monthly`` is pushed to billing after commit for
    every LIVE licence on the plan whose budget is plan-driven (no
    ``token_budget_override``) — see ``push_effective_token_budgets``. The push
    runs after the response; a billing failure never fails this request (same
    contract as licence creation) and is repaired by the reconcile sweep.
    """
    try:
        plan_uuid = UUID(plan_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid plan ID: {plan_id}"
        )

    updates = plan_data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    if "revoked" in updates and updates["revoked"] is None:
        # plans.revoked is NOT NULL; an explicit null would otherwise surface as a 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="revoked must be true or false"
        )

    try:
        existing = db_manager.get_plan(plan_uuid)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
        _authorize_app_admin(user, existing.app_id, "plan_update")
        previous_budget = existing.token_budget_monthly
        plan = db_manager.update_plan(plan_uuid, **updates)
        if not plan:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

        logger.info(f"Plan updated: {plan.id} ({plan.code})")
        if "token_budget_monthly" in updates and plan.token_budget_monthly != previous_budget:
            # After commit (update_plan committed above). BackgroundTasks runs
            # the coroutine on the event loop after the response is sent.
            background_tasks.add_task(
                push_effective_token_budgets,
                plan_id=str(plan.id),
                plan_driven_only=True,
                reason=(
                    f"plan {plan.id} token_budget_monthly "
                    f"{previous_budget} -> {plan.token_budget_monthly}"
                ),
            )
        return PlanResponse(**plan.to_dict())

    except HTTPException:
        raise
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to update plan",
            e,
            "Failed to update plan",
        ) from e


@app.get(
    "/plans",
    response_model=list[PlanResponse],
    operation_id="list_plans",
    tags=["plans"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_plans(
    app_id: str | None = Query(None),
    region_id: str | None = Query(None),
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all plans with optional filters

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        app_uuid = UUID(app_id) if app_id else None
        region_uuid = UUID(region_id) if region_id else None

        plans = db_manager.list_plans(app_id=app_uuid, region_id=region_uuid)
        return [PlanResponse(**plan.to_dict()) for plan in plans]

    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "list_plans: Invalid UUID"
        ) from e
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to list plans", e, "Failed to list plans"
        ) from e


@app.get(
    "/plans/{plan_id}",
    response_model=PlanResponse,
    operation_id="get_plan",
    tags=["plans"],
    responses=COMMON_ERROR_RESPONSES,
)
def get_plan(
    plan_id: str,
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    Get plan by ID

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        plan = db_manager.get_plan(UUID(plan_id))

        if not plan:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

        return PlanResponse(**plan.to_dict())

    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "get_plan: Invalid UUID"
        ) from e
    except HTTPException:
        raise
    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to get plan", e, "Failed to get plan"
        ) from e


# ============= Region Endpoints =============


@app.post(
    "/regions",
    response_model=RegionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_region",
    tags=["regions"],
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {
            "model": RegionResponse,
            "description": (
                "Existing region returned unchanged (idempotent — code already registered)"
            ),
        },
    },
)
def create_region(region_data: RegionCreate, user: User = Depends(require_role("license-admin"))):
    """
    Create a new region (idempotent — returns existing region if code matches)

    Requires 'license-admin' role
    """
    try:
        existing = db_manager.get_region_by_code(region_data.code)
        if existing:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=RegionResponse(**existing.to_dict()).model_dump(),
            )

        region = db_manager.create_region(
            code=region_data.code, name=region_data.name, active=region_data.active
        )

        logger.info(f"Region created: {region.id} ({region.code})")

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=RegionResponse(**region.to_dict()).model_dump(),
        )

    except IntegrityError:
        # Same read-then-insert race as ``create_app``, on the unique
        # ``regions.code`` index (models.py). 200-with-the-existing-row because
        # this route's docstring promises "idempotent — returns existing region
        # if code matches"; re-raise when the re-read finds nothing, because then
        # the violated constraint was not the code race.
        existing = db_manager.get_region_by_code(region_data.code)
        if existing is not None:
            logger.info(f"Region {existing.id} ({existing.code}) already exists; returning it")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=RegionResponse(**existing.to_dict()).model_dump(),
            )
        raise
    # Top-level handler: spans DB + JSONResponse build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create region",
            e,
            "Failed to create region",
        ) from e


@app.get(
    "/regions",
    response_model=list[RegionResponse],
    operation_id="list_regions",
    tags=["regions"],
    responses=COMMON_ERROR_RESPONSES,
)
def list_regions(
    active_only: bool = Query(True, description="Only list active regions"),
    user: User = Depends(require_any_role("license-admin", "license-reader")),
):
    """
    List all regions

    Requires 'license-admin' or 'license-reader' role
    """
    try:
        regions = db_manager.list_regions(active_only=active_only)
        return [RegionResponse(**region.to_dict()) for region in regions]

    # Top-level handler: spans DB + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list regions",
            e,
            "Failed to list regions",
        ) from e


# --------------- DELETE endpoints ---------------


@app.delete(
    "/apps/{app_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_app",
    tags=["apps"],
    responses={
        **COMMON_ERROR_RESPONSES,
        **APP_ADMIN_403_RESPONSE,
        409: {
            "model": ErrorResponse,
            "description": (
                "The cascade would destroy licences and was not confirmed. "
                "`cascade_requires_confirmation` — licences exist on this app and "
                "`confirm_licenses` was not supplied; the body carries the exact "
                "`licenses`, `tokens`, `plans` and `tenants` that would be destroyed, "
                "so re-issue the request with `?confirm_licenses=<licenses>`. "
                "`cascade_count_mismatch` — `confirm_licenses` did not equal the live "
                "licence count, i.e. the catalogue changed between the two calls; "
                "re-read the count and retry."
            ),
        },
    },
)
def delete_app(
    app_id: str,
    confirm_licenses: int | None = Query(
        None,
        ge=0,
        description=(
            "Acknowledge the blast radius. An `app` row is GLOBAL — it has no "
            "tenant_id — so deleting it destroys EVERY tenant's licences for that "
            "app (and their tokens, which cascade via the license_id FK). When any "
            "licence exists this must equal the exact live licence count, which a "
            "first unconfirmed call returns in its 409 body."
        ),
    ),
    user: User = Depends(require_role("license-admin")),
):
    """
    Hard-delete an app and cascade to its plans, licenses, and tokens.

    Requires 'license-admin' role AND that the caller's owner issues the app
    (403 ``app_not_administrable_by_caller``, checked after the 404 and before
    the 409; cross_owner bypasses, audited).

    ⚠️ CROSS-TENANT AND IRREVERSIBLE. ``app`` is a global catalogue row (no
    ``tenant_id``; ``key`` is globally unique), and ``license.app_id`` is
    ``ON DELETE CASCADE``, so this destroys the licences of every tenant holding
    the app — not just the caller's. There is deliberately no tenant predicate
    here, because there is no coherent one to add: scoping the licence delete to
    one tenant would leave the app row deleted and the database would cascade the
    other tenants' licences anyway, so the "scope" would be decorative while the
    surviving plan rows tripped ``license.plan_id``'s RESTRICT.

    The real guard is therefore blast-radius confirmation, mirroring
    ``scripts/purge_test_apps.py``: an unconfirmed call that would destroy
    licences REFUSES with 409 and reports exactly what it would have destroyed.
    """
    try:
        try:
            uid = UUID(app_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid app_id format"
            )

        radius = db_manager.app_cascade_radius(uid)
        if radius is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
        # Before the 409: a non-issuer must not learn the blast radius either.
        _authorize_app_admin(user, uid, "app_delete")

        if radius["licenses"] and confirm_licenses != radius["licenses"]:
            # Fail CLOSED. The destruction is unrecoverable and this service has no
            # audit table, so the refusal — and the counts in it — are the only
            # chance an operator gets to see the radius before it happens.
            logger.warning(
                "app_delete_refused: id=%s reason=%s licenses=%d tokens=%d plans=%d "
                "tenants=%d confirm_licenses=%s actor=%s",
                uid,
                "cascade_count_mismatch" if confirm_licenses is not None else "unconfirmed",
                radius["licenses"],
                radius["tokens"],
                radius["plans"],
                len(radius["tenants"]),
                confirm_licenses,
                user.sub,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": (
                        "cascade_count_mismatch"
                        if confirm_licenses is not None
                        else "cascade_requires_confirmation"
                    ),
                    "app_id": str(uid),
                    "licenses": radius["licenses"],
                    "tokens": radius["tokens"],
                    "plans": radius["plans"],
                    "tenants": radius["tenants"],
                    "remedy": (
                        f"re-issue with ?confirm_licenses={radius['licenses']} to destroy "
                        f"{radius['licenses']} licence(s) across "
                        f"{len(radius['tenants'])} tenant(s)"
                    ),
                },
            )

        if radius["licenses"]:
            # ERROR: a confirmed cross-tenant hard delete is the single most
            # destructive thing this API can do and nothing else records it.
            logger.error(
                "app_delete_confirmed: id=%s licenses=%d tokens=%d plans=%d tenants=%s actor=%s",
                uid,
                radius["licenses"],
                radius["tokens"],
                radius["plans"],
                ",".join(radius["tenants"]),
                user.sub,
            )

        if not db_manager.delete_app(uid):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB cascade delete; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to delete app", e, "Failed to delete app"
        ) from e


@app.delete(
    "/plans/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_plan",
    tags=["plans"],
    responses={
        **COMMON_ERROR_RESPONSES,
        **APP_ADMIN_403_RESPONSE,
        409: {
            "model": ErrorResponse,
            "description": "Cannot delete plan: licenses still reference it",
        },
    },
)
def delete_plan(
    plan_id: str,
    user: User = Depends(require_role("license-admin")),
):
    """
    Hard-delete a plan. Fails with 409 if licenses still reference it.

    Requires 'license-admin' role AND that the caller's owner issues the plan's
    app (403 ``app_not_administrable_by_caller``; cross_owner bypasses, audited).
    """
    try:
        try:
            uid = UUID(plan_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan_id format"
            )

        existing = db_manager.get_plan(uid)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
        _authorize_app_admin(user, existing.app_id, "plan_delete")

        found, reason = db_manager.delete_plan(uid)
        if not found and reason == "conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete plan: licenses still reference it",
            )
        if not found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB delete; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to delete plan",
            e,
            "Failed to delete plan",
        ) from e


class DeleteLicensesResponse(BaseModel):
    deleted: int
    #: ``"hard"`` when the rows were destroyed, ``"soft"`` when they were retained
    #: and stamped. Published so a caller can tell which happened WITHOUT knowing
    #: this service's flag state — a count of 5 means two very different things.
    mode: str = "hard"
    #: Soft mode only: the tenant's dependency licences released in the same
    #: transaction because no remaining active licence requires them.
    released_dependency_ids: list[str] = Field(default_factory=list)


@app.delete(
    "/licenses/user/{subject_id}",
    response_model=DeleteLicensesResponse,
    operation_id="delete_user_licenses",
    tags=["licenses"],
    responses={
        **COMMON_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": (
                "The deletion was not confirmed, and only when `LICENSE_SOFT_DELETE` is "
                "armed. `licence_delete_requires_confirmation` — licences exist for this "
                "(tenant, subject) and `confirm_licenses` was not supplied; the body "
                "carries the exact `licenses`, `active_licences` and `tokens` that would "
                "be affected, so re-issue with `?confirm_licenses=<licenses>`. "
                "`licence_delete_count_mismatch` — `confirm_licenses` did not equal the "
                "live count, i.e. the rows changed between the two calls; re-read and "
                "retry."
            ),
        },
    },
)
def delete_user_licenses(
    subject_id: str,
    request: Request,
    confirm_licenses: int | None = Query(
        None,
        ge=0,
        description=(
            "Acknowledge the blast radius, exactly as `DELETE /apps/{app_id}` does. Must "
            "equal the live licence count for this (tenant, subject) when any exists; a "
            "first unconfirmed call returns that count in its 409 body. ⚠️ REQUIRED "
            "WHENEVER `LICENSE_SOFT_DELETE` IS ARMED, which is now the DEFAULT — only a "
            "deployment that explicitly sets `LICENSE_SOFT_DELETE=false` keeps the "
            "historical unconfirmed hard-delete, and there this parameter is ignored."
        ),
    ),
    reason: str | None = Query(
        None,
        max_length=500,
        description=(
            "Why the licences are being deleted. Recorded on the retained rows "
            "(`license.delete_reason`) on the armed path, which is the default; there is "
            "no audit table in this service, so this is the only place the WHY of a "
            "destructive request is kept. ⚠️ It is no longer discarded on the hard path "
            "either — with `LICENSE_SOFT_DELETE=false` there is no row to stamp, so it is "
            "carried on the `licence_delete_confirmed … mode=hard` ERROR log line instead."
        ),
    ),
    tenant_id: str = Depends(require_tenant_id),
    user: User = Depends(require_role("license-admin")),
):
    """
    Delete all licenses (and their tokens) for a subject within a tenant.

    Requires 'license-admin' role.

    ⛔ WHAT THIS DOES DEPENDS ON `LICENSE_SOFT_DELETE` (default **True**).

    **Flag off — the historical destroy, now opt-in and no longer silent.** A bare
    `.delete()`: the rows go and their tokens go with them via `token.license_id`
    ON DELETE CASCADE, and no confirmation is required. What changed is that the
    blast radius is read BEFORE the delete and written to a
    `licence_delete_confirmed … mode=hard` ERROR line carrying actor, counts and
    reason — because once the rows are gone there is nothing left to be the record,
    and this service has no audit table to write to instead.

    **Flag on (DEFAULT) — audited soft delete.** The rows are RETAINED and stamped
    (`deleted_at`, `deleted_by`, `delete_reason`, `deleted_from_status`; migration
    036), `status` becomes `revoked` and every unrevoked token on them is revoked in
    the same transaction. A blast-radius confirmation is then required, mirroring
    `delete_app`.

    ⭐ WHY RETAINING THE ROW IS THE POINT, NOT TIDINESS.
    `GET /internal/tenants/{tenant_id}/seat-count?as_of=` is the DENOMINATOR of
    billing's per-seat access charge, and its own `as_of` description admits it
    "cannot reconstruct a licence that has since been deleted" — it can only filter
    rows that still exist. So a hard delete makes every CLOSED period's seat count
    silently re-derive SMALLER than the invoice was written from, with nothing left
    to explain the gap. `effectively_active_clauses` reads `deleted_at`, so a
    retained row is still counted for any `as_of` earlier than its deletion and is
    excluded from every present-tense answer.

    ⚠️ TOKENS ARE REVOKED EXPLICITLY ON THE SOFT PATH BECAUSE THE CASCADE NO LONGER
    FIRES. The row survives, so the FK does not remove its tokens; without the
    explicit revoke a "deleted" licence would keep authorizing calls. That is
    handled in `soft_delete_licenses_for_subject`, inside the same transaction as
    the stamp, so the two cannot separate.

    ⭐ WHY THE FLAG NOW DEFAULTS **ON**, AND WHAT HAD TO LAND FIRST. It used to
    default off, and the live deployment sets no value — so production ran the
    destroying path. The stated blocker was real: a retained row was still returned
    by `GET /internal/licenses` and `GET /licenses`, because nothing filtered
    `deleted_at` on the list surfaces, so arming this would have made deleted
    licences REAPPEAR to their consumers, billing included. That is closed —
    `list_licenses` takes `include_deleted` (default False) and both list surfaces
    expose it — so an armed soft delete now shows those consumers exactly what a
    hard delete showed them, and the retained row is reachable for audit by asking
    for it. Read half and write half moved together; neither is valid alone.
    """
    try:
        # Resolve subject (same logic as list_user_licenses)
        subject = db_manager.get_subject_by_keycloak_sub(subject_id)
        if not subject:
            try:
                subject = db_manager.get_subject(UUID(subject_id))
            except ValueError:
                pass

        if not subject:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

        resolved_subject_id = UUID(str(subject.id))

        # Migration 050: deleting a subject's licences on a tenant is a licence
        # write, so the caller must be allowed to issue EVERY app it would touch,
        # on a tenant it owns or holds a grant on.
        # For an app the caller does NOT issue, the tenant's owner is still
        # admitted when every doomed row of that app is bundle-minted
        # (``_bundle_owner_may_revoke``); one non-bundle row of such an app
        # refuses the whole call, so this never reaches a non-bundle row.
        doomed = db_manager.list_licenses(subject_id=resolved_subject_id, tenant_id=tenant_id)
        _require_known_owner(user)
        _authorize_licence_revoke(user, doomed, tenant_id, "licence_delete")
        # Read BEFORE the write: on the hard path the rows are gone afterwards.
        # Every one of them owes billing the same two teardowns a single revoke
        # does (quota config + token budget); without them billing kept the
        # budgets active (`licence_budget_divergence_detected_not_repaired`).
        doomed_ids = [str(lic.id) for lic in doomed]

        if not LICENSE_SOFT_DELETE:
            # ⛔ THE OPT-OUT PATH, reachable only by setting LICENSE_SOFT_DELETE
            # false. Still unconfirmed, and deliberately so: a caller who has
            # explicitly disarmed retention must not additionally be broken by a
            # new required parameter. What it is no longer is UNRECORDED.
            #
            # ⚠️ THE RADIUS IS READ BEFORE THE DELETE, AND THAT ORDERING IS THE
            # WHOLE RECORD. After `.delete()` the rows are gone and their tokens
            # with them (`token.license_id` ON DELETE CASCADE), so every number
            # worth recording — how many licences, how many were ACTIVE, how many
            # tokens died — is unobtainable a line later. A log line written
            # afterwards could only say "some rows were deleted", which is not an
            # audit trail, it is a rumour.
            radius = db_manager.subject_licence_delete_radius(
                subject_id=resolved_subject_id, tenant_id=tenant_id
            )
            count = db_manager.delete_licenses_for_subject(
                subject_id=resolved_subject_id, tenant_id=tenant_id
            )
            if count:
                # ⛔ THE LOG LINE IS THE AUDIT ROW, because on this path there is
                # no row left to be one. ERROR, at the same level and in the same
                # shape as the soft path's `licence_delete_confirmed` and
                # `delete_app`'s cascade line — this service has NO audit table
                # (see `soft_delete_licenses_for_subject`), and inventing one here
                # would give the estate a second, half-populated mechanism for a
                # question the retained row already answers everywhere else.
                #
                # ⚠️ It carries WHO (`user.sub`, the authenticated actor from the
                # `license-admin` dependency), WHAT (subject, tenant, the three
                # counts, `reason`) and WHEN (the log record's own timestamp).
                # `mode=hard` is the field that matters most: it is the only thing
                # that distinguishes this line from the soft path's, where the
                # rows can still be inspected.
                #
                # ⚠️ `reason` is NOT ignored on this path any more. Its parameter
                # description used to say it was, because there was nowhere to put
                # it once the row was destroyed; there is now.
                logger.error(
                    "licence_delete_confirmed: subject=%s tenant=%s mode=hard deleted=%d "
                    "active=%d tokens=%d reason=%r actor=%s "
                    "(IRREVERSIBLE — rows destroyed, tokens cascaded, no audit table; "
                    "this line is the only record and every seat count for a closed "
                    "period is now unreproducible for this subject)",
                    resolved_subject_id,
                    tenant_id,
                    count,
                    radius["active_licences"],
                    radius["tokens"],
                    reason,
                    user.sub,
                )
            if count:
                _spawn_billing_teardowns_from_thread(doomed_ids, tenant_id)
            return DeleteLicensesResponse(deleted=count, mode="hard")

        radius = db_manager.subject_licence_delete_radius(
            subject_id=resolved_subject_id, tenant_id=tenant_id
        )
        if radius["licenses"] and confirm_licenses != radius["licenses"]:
            # Fail CLOSED, mirroring delete_app: the refusal — and the counts in
            # it — are the operator's only sight of the radius beforehand.
            # `active_licences` is reported separately because that is the number
            # that moves a seat count and therefore an invoice.
            logger.warning(
                "licence_delete_refused: subject=%s tenant=%s reason=%s licenses=%d "
                "active=%d tokens=%d confirm_licenses=%s actor=%s",
                resolved_subject_id,
                tenant_id,
                "licence_delete_count_mismatch" if confirm_licenses is not None else "unconfirmed",
                radius["licenses"],
                radius["active_licences"],
                radius["tokens"],
                confirm_licenses,
                user.sub,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": (
                        "licence_delete_count_mismatch"
                        if confirm_licenses is not None
                        else "licence_delete_requires_confirmation"
                    ),
                    "subject_id": str(resolved_subject_id),
                    "tenant_id": tenant_id,
                    "licenses": radius["licenses"],
                    "active_licences": radius["active_licences"],
                    "tokens": radius["tokens"],
                    "remedy": (
                        f"re-issue with ?confirm_licenses={radius['licenses']} to delete "
                        f"{radius['licenses']} licence(s), {radius['active_licences']} of "
                        f"them active"
                    ),
                },
            )

        released: list[str] = []
        count = db_manager.soft_delete_licenses_for_subject(
            subject_id=resolved_subject_id,
            tenant_id=tenant_id,
            actor=user.sub,
            reason=reason,
            released_out=released,
        )
        if count:
            # ERROR: a confirmed deletion of licences is the most billing-visible
            # thing this route can do, and `delete_app` logs its confirmed cascade
            # at the same level for the same reason. Unlike there, the rows survive
            # — so this line is corroboration, not the sole record.
            logger.error(
                "licence_delete_confirmed: subject=%s tenant=%s mode=soft deleted=%d "
                "active=%d tokens=%d reason=%r actor=%s",
                resolved_subject_id,
                tenant_id,
                count,
                radius["active_licences"],
                radius["tokens"],
                reason,
                user.sub,
            )
        if count:
            _spawn_billing_teardowns_from_thread(
                doomed_ids + [d for d in released if d not in doomed_ids], tenant_id
            )
        return DeleteLicensesResponse(deleted=count, mode="soft", released_dependency_ids=released)

    except HTTPException:
        raise
    # Top-level handler: spans subject lookup + DB cascade delete; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to delete licenses",
            e,
            "Failed to delete licenses",
        ) from e


# ============= Internal Service Endpoints =============


class InternalLicenseResponse(BaseModel):
    """Enriched license response for internal service consumers"""

    id: str
    tenant_id: str
    subject_id: str
    #: The STORED ``status`` column, verbatim. ⚠️ NOT VALIDITY — nothing in the
    #: estate transitions it out of ``'active'`` when ``expires_at`` passes, so it
    #: drifts, and only ever in the over-counting direction. Read
    #: ``effective_status`` to decide whether a row is a seat.
    status: str
    #: The TIME-AWARE verdict for the same row: ``active`` | ``pending`` |
    #: ``expired`` | ``revoked``, from ``License.effective_status()``. Published
    #: because this endpoint and ``/internal/tenants/{id}/seat-count`` are BOTH
    #: read by billing for the SAME invoice, and a consumer holding only the stored
    #: column had to re-derive this service's own rule in another repo to reconcile
    #: them — which is how the two came to disagree.
    effective_status: str
    starts_at: str
    expires_at: str | None
    metadata: dict[str, Any] | None
    #: Migration 059. TRUE = a DEPENDENCY licence: provisioned only because a bundled
    #: app requires this app, internal-only (bundle-token), never a user token.
    #: Billing must not infer a billed licence from it.
    is_dependency: bool = False
    #: Migration 041 (#239). This licence's OWN monthly token budget. When not
    #: None it WINS over ``plan.token_budget_monthly``; consumers must use
    #: ``token_budget_override if token_budget_override is not None else
    #: plan.token_budget_monthly`` (``models.effective_metering_values``). A
    #: consumer reading only the plan value over-grants (billing sync-from-licenses
    #: set 50,000,000 on a licence whose override is 100,000).
    token_budget_override: int | None = Field(
        default=None,
        description=(
            "Per-licence monthly token budget; wins over plan.token_budget_monthly. "
            "Consumers must use `token_budget_override if not None else "
            "plan.token_budget_monthly`."
        ),
    )
    #: Migration 041 (#239). This licence's OWN monthly request quota; same rule
    #: as ``token_budget_override`` against ``plan.quota_monthly``.
    quota_override: int | None = Field(
        default=None,
        description=(
            "Per-licence monthly request quota; wins over plan.quota_monthly. "
            "Consumers must use `quota_override if not None else plan.quota_monthly`."
        ),
    )
    app: AppInfo | None = None
    plan: PlanInfo | None = None


@app.get(
    "/internal/licenses",
    response_model=list[InternalLicenseResponse],
    operation_id="internal_list_licenses",
    tags=["internal"],
    responses=COMMON_ERROR_RESPONSES,
)
def internal_list_licenses(
    tenant_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    app_id: str | None = Query(None),
    plan_id: str | None = Query(None),
    effective: bool = Query(
        True,
        description=(
            "What `status=active` MEANS. `true` (default) applies the same three-part "
            "seat rule as `/internal/tenants/{id}/seat-count` — stored-active AND started "
            "AND unexpired, at `as_of`. `false` filters the STORED `status` column "
            "verbatim, which is what an operator auditing column drift wants and what "
            "this endpoint used to do unconditionally. Only affects `status=active`; the "
            "terminal statuses are time-invariant."
        ),
    ),
    as_of: datetime | None = Query(
        None,
        description=(
            "Evaluate the window at this instant instead of now. Ignored unless "
            "`effective` is set. Same semantics as `/internal/tenants/{id}/seat-count`'s "
            "`as_of`. ⚠️ Its old description said it 'cannot reconstruct a deleted "
            "licence'; with `LICENSE_SOFT_DELETE` armed (now the default) a deleted "
            "licence is RETAINED, so `effective=true` does resolve it correctly at a past "
            "instant — that is the reproducibility the soft delete exists to buy."
        ),
    ),
    include_deleted: bool = Query(
        False,
        description=(
            "Include soft-deleted licences (`deleted_at` set) in an UNFILTERED or "
            "stored-status listing. `false` (default) matches what this endpoint returned "
            "before `LICENSE_SOFT_DELETE` was armed. ⚠️ IGNORED when `effective=true` and "
            "`status=active`: that path resolves the deleted case as a function of "
            "`as_of` (a licence deleted AFTER `as_of` was still a seat AT `as_of`), and "
            "layering this filter on top would silently shrink a closed period's answer."
        ),
    ),
    _user: User = Depends(require_internal_service),
):
    """
    List licenses with enriched app and plan data.

    For internal service consumers (billing, onboarding).
    Requires a service-account JWT carrying one of this service's
    INTERNAL_SERVICE_ROLES: service-internal, onboarding-internal or
    license-admin (see auth/dependencies.py:require_internal_service).

    ⚠️ CORRECTED 2026-08-10: this used to name `metering-internal`, which was
    rotated fleet-wide to `service-internal` and retired here. A caller
    presenting only `metering-internal` is refused with 403.

    ⛔ `?status=active` NOW MEANS THE SAME THING IT MEANS ON THE SEAT-COUNT
    ENDPOINT, AND THAT IS A DELIBERATE BEHAVIOUR CHANGE. It used to filter the
    stored `status` column alone, while `/internal/tenants/{id}/seat-count`
    applied the three-part time-aware rule — so one row that is stored-active but
    past its `expires_at` was a seat on one endpoint and not on the other, and
    **billing reads both, for the same invoice**. Both now call
    `models.effectively_active_clauses`; there is one predicate and it is not
    written out twice.

    ⚠️ WHY THE DEFAULT IS `effective=true` AND WHY THAT IS SAFE TO LAND. It is the
    answer billing needs, and making a biller opt IN to correctness leaves the
    defect live until another repo changes. It is safe because the widening is
    currently EMPTY: measured 2026-09-10 on the primary there were ZERO
    stored-active-but-past-expiry rows, so on today's data the two settings return
    identical sets. `effective=false` remains for the operator who is specifically
    looking for that drift — and `effective_status` is now on every row either
    way, so the drift is visible without changing the filter at all.

    ⚠️ The two ADMIN list routes (`GET /licenses`, `GET /licenses/admin/all`) are
    deliberately UNCHANGED and still filter the stored column: an operator
    debugging a licence must be able to see a row whose column disagrees with the
    clock. Hiding it there is how the drift became invisible in the first place.
    """
    try:
        app_uuid = UUID(app_id) if app_id else None
        plan_uuid = UUID(plan_id) if plan_id else None

        # tenant_id is an UNVALIDATED query param filtered against a UUID column
        # (migration 016). A non-UUID value would raise InvalidTextRepresentation
        # (500) in the DB; normalize a non-UUID to "no match" (empty list) — same
        # guard as get_active_license.
        if tenant_id is not None:
            try:
                UUID(str(tenant_id))
            except ValueError:
                return []

        licenses = db_manager.list_licenses(
            app_id=app_uuid,
            plan_id=plan_uuid,
            status=status_filter,
            tenant_id=tenant_id,
            effective=effective,
            as_of=as_of,
            include_deleted=include_deleted,
        )

        results = []
        for lic in licenses:
            data: dict[str, Any] = {
                "id": str(lic.id),
                # tenant_id is a UUID column (migration 016); this response builds
                # the dict INLINE (not via License.to_dict), so str() it here too —
                # InternalLicenseResponse.tenant_id is `str` and Pydantic rejects a
                # raw UUID (ValidationError → the except ValueError → a bogus 400).
                "tenant_id": str(lic.tenant_id),
                "subject_id": str(lic.subject_id),
                "status": lic.status,
                # Derived from the row, never stored, so it cannot drift from the
                # column it corrects — the same rule the seat count applies.
                "effective_status": lic.effective_status(),
                "starts_at": lic.starts_at.isoformat() if lic.starts_at else None,
                "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
                "metadata": lic.license_metadata,
                # migration 059: billing's licence mirror must exclude dependency
                # licences from licence inference (billing #274).
                "is_dependency": bool(lic.is_dependency),
                # migration 041: the licence's own limits WIN over the plan's —
                # billing's sync-from-licenses must not see only the plan value.
                "token_budget_override": lic.token_budget_override,
                "quota_override": lic.quota_override,
            }
            if lic.app:
                data["app"] = AppInfo(
                    id=str(lic.app.id),
                    key=lic.app.key,
                    name=lic.app.name,
                )
            if lic.plan:
                data["plan"] = PlanInfo(
                    id=str(lic.plan.id),
                    code=lic.plan.code,
                    name=lic.plan.name,
                    rate_limit_rpm=lic.plan.rate_limit_rpm,
                    rate_limit_burst=lic.plan.rate_limit_burst,
                    quota_monthly=lic.plan.quota_monthly,
                    token_budget_monthly=lic.plan.token_budget_monthly,
                )
            results.append(InternalLicenseResponse(**data))

        return results

    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "internal_list_licenses: Invalid UUID"
        ) from e
    # Top-level handler: spans DB + enriched response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list internal licenses",
            e,
            "Failed to list internal licenses",
        ) from e


class InternalTenantResponse(BaseModel):
    """Tenant identity (uuid -> human name) for internal service consumers"""

    id: str
    name: str
    keycloak_realm: str | None
    archived: bool
    # `kind` and `cost_centre` (migration 027) travel HERE because this is the one
    # tenant endpoint billing can actually call: GET /tenants requires
    # license-admin/license-reader and billing's service account is refused with
    # 403 (measured in the pod 2026-08-28, codified in
    # billing_service/src/services/attribution_audit.py). Without these two
    # fields "cost by tenant kind" needs a crosswalk maintained by hand in
    # billing_service/src/tenancy.py, which is how the registry and the ledger
    # drifted apart in the first place.
    kind: str
    cost_centre: str | None
    # Migration 043: null = token budget, else the currency billing holds the
    # tenant's MONEY budget in. Same value as TenantResponse.budget_currency.
    budget_currency: BudgetCurrency | None = None
    # Migration 050: the app that owns the tenant. Names are unique only per
    # owner now, so a bare name no longer identifies a tenant — billing must key
    # on `id` and can use this to disambiguate a name it still holds.
    owner_key: str
    external_ref: str | None = None


class InternalAppResponse(BaseModel):
    """App identity (uuid <-> registry key) for internal service consumers"""

    id: str
    key: str
    name: str
    revoked: bool
    audience: Literal["human", "platform", "automation"]


class InternalSeatCountResponse(BaseModel):
    """A tenant's billable seat count at a point in time.

    Consumed by billing-service's per-seat access charge (``services/seats.py``).
    ``seats`` is the number billed TODAY; the subject counts are beside it because
    they are not the same number and choosing between them is a commercial
    decision — see ``DatabaseManager.count_tenant_seats``.

    ⭐ THE OPERATOR CHOSE A PERSON AS THE BASIS (George, 2026-09-11), and the field
    that implements that choice is ``human_subjects`` — NOT ``subjects``, which
    counts machine accounts as colleagues. ``service_subjects`` discloses exactly
    what was excluded, and ``unresolved_subjects`` keeps an unknown from being
    promoted to either. The three partition ``subjects`` exactly.
    """

    tenant_id: str
    #: Active, started, unexpired licences — the basis billing reads today.
    seats: int
    #: DISTINCT subjects behind those licences. ⚠️ A PRINCIPAL, NOT A PERSON: this
    #: includes machine accounts (measured 2026-09-11: ahtapol's 2 subjects span
    #: realms `portal` and `service`, one being `svc-workers-ahtapol`). Lower than
    #: ``seats`` whenever one principal holds more than one product. Kept
    #: unchanged so the basis choice stays auditable against the old number.
    subjects: int
    #: ``subjects`` with machine accounts REMOVED — people only, and the count that
    #: matches the operator's person basis. Subjects whose Keycloak realm is in
    #: ``models.NON_HUMAN_SUBJECT_REALMS`` are excluded.
    human_subjects: int
    #: What ``human_subjects`` excluded: distinct service-realm subjects holding a
    #: counted licence. Published so the gap between ``subjects`` and
    #: ``human_subjects`` is never a number the caller has to re-derive.
    service_subjects: int
    #: Distinct ``subject_id`` values with no matching ``subject`` row. ALWAYS 0
    #: today (`license.subject_id` is NOT NULL with an FK ON DELETE CASCADE); it
    #: exists so that an unknown principal can never be counted as a human or as a
    #: robot by an accident of join type. Nonzero = schema regression, not a seat.
    unresolved_subjects: int
    #: ``active_licences`` normally; ``tenant_archived`` when the tenant is off,
    #: so a 0 can be told apart from "has no seats".
    basis: str
    tenant_kind: str
    tenant_archived: bool
    as_of: datetime
    #: The product the count is scoped to (``?app=``); ``None`` = every app, the
    #: original behaviour. Echoed so a stored count states its own basis.
    app_key: str | None = None
    #: Set together when the count is over a period (``basis='active_during_period'``).
    period_start: datetime | None = None
    period_end: datetime | None = None


@app.get(
    "/internal/tenants/{tenant_id}/seat-count",
    response_model=InternalSeatCountResponse,
    operation_id="internal_tenant_seat_count",
    tags=["internal"],
    responses=COMMON_ERROR_RESPONSES,
)
def internal_tenant_seat_count(
    tenant_id: str,
    as_of: datetime | None = Query(
        None,
        description=(
            "Count at this instant instead of now. A CLOSED billing period must "
            "be counted at its own instant, never at whenever the query is "
            "re-run. ⚠️ `license` has no history table, so this filters the "
            "CURRENT rows by their own starts_at/expires_at — it cannot "
            "reconstruct a licence that has since been deleted. billing-service "
            "therefore freezes the count it receives into the invoice snapshot; "
            "that frozen number, not this endpoint, is the historical record."
        ),
    ),
    app: str | None = Query(
        None,
        description=(
            "Scope the count to ONE product by app key (e.g. 'commonpractice'). Without it "
            "a tenant's licences for every other app inflate a per-product seat count. An "
            "unknown key is 422 `app_not_found` — deliberately NOT 404, which billing's "
            "seats client reads as a durable 0."
        ),
    ),
    period_start: AwareDatetime | None = Query(
        None,
        description=(
            "With `period_end`: count distinct seats held at ANY instant in "
            "[period_start, period_end) — a mid-period joiner and leaver each count. "
            "Uses a period predicate that honours revocation time; `as_of` is ignored."
        ),
    ),
    period_end: AwareDatetime | None = Query(None, description="See `period_start`."),
    _user: User = Depends(require_internal_service),
):
    """Count a tenant's billable seats.

    ⭐ WHY THIS ENDPOINT EXISTS RATHER THAN BILLING COUNTING `/internal/licenses`:
    that list carries neither tenant `kind` nor `archived_at`, and 156 of the 164
    rows with `status='active'` belong to internal/service/test tenants. Counting
    it per-tenant in billing would invent roughly $3,900/month of seat revenue at
    $25/seat. The predicate belongs next to the columns that define it.

    A seat is a licence that is stored-active AND started AND unexpired — the same
    three-part rule as `License.effective_status`, evaluated against `as_of`.
    `status='active'` alone is NOT validity: nothing in the estate transitions the
    column when `expires_at` passes.

    ⭐ A SEAT IS A PERSON (George, 2026-09-11) — so the field to bill on is
    `human_subjects`, not `subjects`. `subjects` counts distinct authenticated
    PRINCIPALS, which includes machine accounts: measured on the primary
    2026-09-11, `ahtapol` and `cdmsfirst` each have subjects spanning realms
    `portal` AND `service` (one is `svc-workers-ahtapol`, a robot), so 2 of the 5
    customer tenants would be billed $25/month for a service account. 58 of the
    254 `subject` rows estate-wide are in realm `service`.

    `service_subjects` publishes exactly what `human_subjects` excluded, and
    `unresolved_subjects` counts subject ids with no `subject` row (always 0 today)
    so an unknown is never promoted into either side. The three sum to `subjects`.

    ⚠️ `subject.tenant_id` is NOT the way to count a tenant's people — 203 of 254
    rows have it NULL. The authoritative link is `license.subject_id → subject.id`.

    Requires a service-account JWT carrying one of INTERNAL_SERVICE_ROLES
    (service-internal, onboarding-internal, license-admin).
    """
    try:
        result = db_manager.count_tenant_seats(
            tenant_id,
            as_of=as_of,
            app_key=app,
            period_start=period_start,
            period_end=period_end,
        )
    except ClientSafeError as e:
        # A malformed period (one bound, or start >= end) is a CLIENT fault.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    # Top-level handler: spans DB read + response build; any unexpected error is a 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to count tenant seats",
            e,
            "Failed to count tenant seats",
        ) from e
    if result is None:
        # ⚠️ 404, NEVER `{"seats": 0}`. An unknown tenant and a tenant with no
        # seats are different facts, and billing's seats client relies on the
        # distinction: it treats 404 as a durable answer (0, cacheable) and any
        # 5xx as "ask again", so collapsing them here would make an outage
        # indistinguishable from an absence.
        # Plain HTTPException, not `_internal_error`: an unknown tenant is a
        # CLIENT fault, and routing it through the 500 helper would log it at
        # ERROR with a traceback — caller-triggerable log volume on a chain with
        # log-level-based health checks (see `_client_error`'s docstring).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if result.get("app_not_found"):
        # ⛔ 422, NOT 404. The 404 above is billing's "durable 0 seats" signal; an unknown
        # app key is a caller typo, and answering it with the same status would turn a typo
        # into a zero-seat invoice line.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"app_not_found: no app with key {result.get('app_key')!r}",
        )
    return result


@app.get(
    "/internal/tenants",
    response_model=list[InternalTenantResponse],
    operation_id="internal_list_tenants",
    tags=["internal"],
    responses=COMMON_ERROR_RESPONSES,
)
def internal_list_tenants(
    include_archived: bool = Query(False),
    _user: User = Depends(require_internal_service),
):
    """
    List tenants as a uuid -> human-name identity map.

    For internal service consumers — billing reads this to resolve a tenant
    UUID to its name when rendering the per-tenant token/cost ledger.
    `billing_cost_daily.tenant_name` holds the raw uuid string for all but a
    handful of tenants because the emitters only ever see the uuid, and billing
    keeps its own database (one service, one database), so the name has to be
    fetched over HTTP rather than joined.

    Archived tenants are EXCLUDED by default and returned only with
    `include_archived=true`: a retired tenant still owns historical cost rows,
    so a backfill over an old window has to be able to name it.

    Requires a service-account JWT carrying one of this service's
    INTERNAL_SERVICE_ROLES: service-internal, onboarding-internal or
    license-admin (see auth/dependencies.py:require_internal_service).
    """
    try:
        results: list[dict[str, Any]] = []
        # include_service_actors=True, ALWAYS. This endpoint is an IDENTITY MAP,
        # not a pick-list: billing calls it to resolve a tenant uuid to a name for
        # rows it has already billed, and 33 of the 59 live rows are service
        # actors that emit real on-behalf spend. Inheriting list_tenants' pick-list
        # default here would leave those rows unnameable in the cost ledger — the
        # exact failure this endpoint exists to prevent.
        for t in db_manager.list_tenants(
            include_archived=include_archived, include_service_actors=True
        ):
            # Built as a dict, not InternalTenantResponse(...), for the same
            # reason internal_list_licenses does: the ORM attributes type as
            # `Column[str]`, which the type checker refuses to pass to a `str`
            # field. `response_model` still validates and filters the shape.
            results.append(
                {
                    # tenants.id is a UUID column and InternalTenantResponse.id
                    # is `str`; Pydantic rejects a raw UUID there. Same trap as
                    # internal_list_licenses' tenant_id — str() it at the edge.
                    "id": str(t.id),
                    "name": t.name,
                    "keycloak_realm": t.keycloak_realm,
                    "archived": t.archived_at is not None,
                    "kind": t.kind,
                    "cost_centre": t.cost_centre,
                    "budget_currency": t.budget_currency,
                    "owner_key": t.owner_key,
                    "external_ref": t.external_ref,
                }
            )
        return results

    # Top-level handler: spans DB read + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list internal tenants",
            e,
            "Failed to list internal tenants",
        ) from e


@app.get(
    "/internal/apps",
    response_model=list[InternalAppResponse],
    operation_id="internal_list_apps",
    tags=["internal"],
    responses=COMMON_ERROR_RESPONSES,
)
def internal_list_apps(
    include_revoked: bool = Query(False),
    _user: User = Depends(require_internal_service),
):
    """
    List EVERY registered app as a uuid <-> key identity map.

    For internal service consumers — billing mirrors this to resolve the app
    KEY a caller sends in ``X-App-Id`` (``core-app``, ``chat-bridge-rmt``) to
    the registry app uuid. It must be the app registry, not the licences: a
    platform app registered by ``scripts/seed_apps.py`` that holds no licence
    (``core-app``, ``chat-bridge*``) never appears in ``/internal/licenses``,
    so a key map built from licences cannot resolve it. ``GET /apps`` lists
    the same rows but requires ``license-admin``/``license-reader``, which
    billing's service account does not (and should not) hold.

    Revoked apps are excluded by default and returned only with
    ``include_revoked=true``, flagged ``revoked: true``: a retired app still
    owns its historical spend, so the identity map must be able to name it.

    Requires a service-account JWT carrying one of this service's
    INTERNAL_SERVICE_ROLES (see auth/dependencies.py:require_internal_service).
    """
    try:
        # Built as dicts for the same reason internal_list_tenants does: the ORM
        # attributes type as ``Column[...]``; ``response_model`` still validates.
        return [
            {
                "id": str(a.id),
                "key": a.key,
                "name": a.name,
                "revoked": bool(a.revoked),
                "audience": a.audience,
            }
            for a in db_manager.list_apps(include_revoked=include_revoked)
        ]

    # Top-level handler: spans DB read + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to list internal apps",
            e,
            "Failed to list internal apps",
        ) from e


#: Hard cap on a resolver-minted bundle token — the same ceiling as an operator token.
BUNDLE_TOKEN_MAX_TTL_MINUTES = OPERATOR_TOKEN_MAX_TTL_MINUTES


class BundleTokenResponse(BaseModel):
    """A short-lived token on ONE per-app licence from the tenant's bundle (raw, once)."""

    token: str
    expires_at: str
    tenant_id: str
    bundle: str
    app: str
    license_id: str
    token_id: str


@app.post(
    "/internal/tenants/{tenant_id}/bundle-token",
    response_model=BundleTokenResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="internal_mint_tenant_bundle_token",
    tags=["internal"],
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {
            "model": ErrorResponse,
            "description": (
                "Fail closed, nothing minted. `detail.error` says which link is missing: "
                "`no_bundle` (tenant unknown, archived or not owned by / granted to the caller's "
                "app, holds no bundle, or does not hold "
                "the one named by `bundle`; `detail.reason` says which), "
                "`app_not_in_bundle` (no held bundle contains `app`), or `no_app_licence` "
                "(the bundle contains `app` but the tenant has no effectively-active "
                "per-app licence carrying that bundle — run "
                "scripts/provision_tenant_bundle.py)."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": (
                "Nothing minted. A missing role, `unknown_api_client`, or "
                "`dependency_grant_scope`: the caller reaches this tenant ONLY through an "
                "app-scoped dependency grant (migration 059) and `app` is not one of its "
                "granted apps (`detail.granted_apps`)."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "Nothing minted. `detail.error` is `ambiguous_bundle` (several held bundles "
                "contain `app` and `bundle` was not given; `detail.bundles` lists them) or "
                "`ambiguous_app_licence` (more than one per-app licence carries the bundle "
                "for this app; `detail.license_ids` lists them — billing cannot attribute "
                "either, so an operator must resolve it)."
            ),
        },
    },
)
def internal_mint_tenant_bundle_token(
    tenant_id: str,
    app_key: str = Query(
        ...,
        alias="app",
        min_length=1,
        max_length=255,
        description=(
            "REQUIRED. The app key the token is for. The token is minted on the tenant's "
            "per-app licence for this app (the one carrying the bundle's id), so it "
            "authorises — and meters against — exactly this app."
        ),
    ),
    ttl_minutes: int = Query(
        15,
        ge=1,
        le=BUNDLE_TOKEN_MAX_TTL_MINUTES,
        description=f"Lifetime in minutes (1..{BUNDLE_TOKEN_MAX_TTL_MINUTES}, default 15).",
    ),
    bundle: str | None = Query(
        None,
        description=(
            "Bundle key to mint under. Optional unless several held bundles contain `app` "
            "(409 `ambiguous_bundle`)."
        ),
    ),
    user: User = Depends(require_bundle_token_minter),
):
    """Resolve (tenant, app) to a SHORT-LIVED token on that app's licence in the bundle.

    Ruling 2026-09-28: a bundle is the provisioning and billing object; runtime usage
    presents the SPECIFIC per-app licence from the tenant's bundle. So this route checks
    the app is in a bundle the tenant holds, selects the tenant's per-app licence with
    that ``bundle_id`` (``scripts/provision_tenant_bundle.py`` guarantees one per
    (tenant, app)), and mints a fresh token on IT through the ordinary ``create_token``
    path. Introspection of the result answers that licence's app only — never the
    bundle's other apps — so every call is metered at (tenant, app, licence). It never
    returns, rotates or revokes an existing token.

    ⛔ ``bundle-token-minter`` realm role ONLY (``require_bundle_token_minter``). The
    shared ``service-internal`` / ``onboarding-internal`` / ``license-admin`` roles do
    NOT admit: ``service-internal`` is held fleet-wide, so it would let any internal
    service mint a live token for ANY tenant. See ``BUNDLE_TOKEN_MINTER_ROLE``. A
    licence-token caller is only ever ``license-reader``, so a minted token cannot
    mint another.
    ⛔ OWNER-SCOPED (migration 054): the caller's app (``api_client``) must own the
    tenant or hold an active grant on it; any other tenant is 404 ``no_bundle``
    (``tenant_not_found``), exactly like an absent one. An unregistered client is
    403 ``unknown_api_client``. cross_owner break-glass reaches any tenant, audited.
    ⛔ SHORT-LIVED: ``ttl_minutes`` ≤ 60, and never past the licence's own expiry.
    Every mint is logged at WARNING as ``bundle_token_minted``.
    """
    try:
        try:
            tenant_uuid = str(UUID(tenant_id))
        except ValueError as e:
            raise _client_error(
                status.HTTP_400_BAD_REQUEST,
                "Invalid tenant_id",
                e,
                "internal_mint_tenant_bundle_token: Invalid UUID",
            ) from e

        def _refuse(code: int, error: str, reason: str, **extra: Any) -> HTTPException:
            logger.warning(
                "bundle_token_refused actor_sub=%s actor_azp=%s tenant_id=%s bundle=%s "
                "app=%s error=%s reason=%s",
                user.sub,
                user.azp,
                tenant_uuid,
                bundle,
                app_key,
                error,
                reason,
            )
            return HTTPException(
                status_code=code,
                detail={
                    "error": error,
                    "tenant_id": tenant_uuid,
                    "app": app_key,
                    "reason": reason,
                    **extra,
                },
            )

        # ⛔ A per-app bundle licence is held by `svc:<tenant>:<app>`, and
        # `subject.keycloak_sub` is VARCHAR(255): an app key past
        # SERVICE_SUBJECT_APP_KEY_MAX_LEN can never have one. Say so, rather than a
        # misleading `no_app_licence` 404.
        too_long = service_subject_app_key_problem(app_key)
        if too_long is not None:
            raise _refuse(status.HTTP_400_BAD_REQUEST, "app_key_too_long", too_long)

        # ⛔ OWNER SCOPE (migration 054). The role alone used to admit a mint on ANY
        # tenant, so every bundle-token-minter holder could mint a live bearer for
        # every other app's customer. The caller's app (api_client) must OWN the
        # tenant or hold an ACTIVE grant on it (the same owner/grantee rule as every
        # other tenant read, ``_resolve_tenant_for_caller``); cross_owner break-glass
        # reaches any tenant, audited as ``cross_owner_action``. An unregistered
        # client is 403 ``unknown_api_client``.
        #
        # Deliberately NOT the issuer rule (``_resolve_tenant_for_app_setting``):
        # minting uses a licence the tenant ALREADY holds, it issues nothing, and a
        # grantee serving the tenant needs tokens for the bundle's other apps —
        # merqi's sales mints ``w2v`` on CommonPractice's netcare (Loki 2026-10-14),
        # and worker_management (platform) mints ``inference-router`` on fr / w2v /
        # CP tenants, none of which their owner issues. Same reasoning as 052's
        # ensurer allowlist ("requiring the caller to ISSUE every bundled app ...
        # would break merqi -> w2v").
        #
        # A tenant the caller cannot see answers EXACTLY like an absent one (404
        # ``no_bundle`` / ``tenant_not_found``): existence is not leaked, and the
        # callers' documented "no bundle credential -> static map" fallback is
        # unchanged for it.
        owner = _require_known_owner(user)
        try:
            tenant = _resolve_tenant_for_caller(user, tenant_uuid, "bundle_token_mint")
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            # ⛔ APP-SCOPED DEPENDENCY GRANT (migration 059). An owner whose minter
            # mints ONE app for this tenant because a bundled app requires it (w2v ->
            # workers on a merqi tenant) holds a ``tenant_dependency_grant`` for
            # exactly (tenant, owner, app) — never a ``tenant_app_grant``, so it
            # cannot read the tenant or issue anything. It admits THIS route for THAT
            # app only; any other app is 403 ``dependency_grant_scope`` (the caller
            # already knows the tenant exists). No dependency grant at all: 404, as
            # for any foreign tenant.
            scope = db_manager.dependency_grant_apps(tenant_uuid, owner)
            if not scope:
                raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "tenant_not_found") from exc
            if app_key not in scope:
                raise _refuse(
                    status.HTTP_403_FORBIDDEN,
                    "dependency_grant_scope",
                    "app_outside_dependency_grant",
                    granted_apps=sorted(scope),
                ) from exc
            scoped = db_manager.get_tenant(tenant_uuid)
            if scoped is None:
                raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "tenant_not_found") from exc
            tenant = scoped
        if str(tenant.id) != tenant_uuid:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "tenant_not_found")
        if tenant.archived_at is not None:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "tenant_archived")

        held = db_manager.list_tenant_bundles(tenant_uuid)
        if bundle is not None:
            held = [b for b in held if str(b.key) == bundle]
            if not held:
                raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "bundle_not_held")
        if not held:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no_bundle", "no_bundle_held")

        # ⛔ THE APP-IN-BUNDLE CHECK. Only bundles whose (non-revoked) apps include
        # `app` — or, since migration 059, whose app_requires CLOSURE includes it —
        # can back this token. Without it, a licence that happened to carry the
        # bundle id for an app since REMOVED from the bundle would still mint.
        #
        # MEMBER app: the per-app licence carrying the bundle's id (unchanged).
        # DEPENDENCY app (in the closure, not a member): the tenant's ONE shared
        # tenant-level licence for the app (bundle_ensure.shared_tenant_licences —
        # the set ensure decided on), wherever it sits. A cycle backs nothing.
        requirement_edges = [Edge(a, r, c) for a, r, c in db_manager.list_app_requirements()]
        dep_app = None

        def _resolve(b: Any) -> list[License] | None:
            nonlocal dep_app
            members = {str(a.key) for a in db_manager.list_bundle_apps(cast(UUID, b.id))}
            if app_key in members:
                return db_manager.find_bundle_app_licences(tenant_uuid, cast(UUID, b.id), app_key)
            try:
                deps, _ = app_requires_closure(members, requirement_edges)
            except DependencyCycleError as e:
                logger.warning(
                    "bundle_token_dependency_cycle tenant_id=%s bundle=%s app=%s: %s",
                    tenant_uuid,
                    b.key,
                    app_key,
                    e,
                )
                return None
            if app_key not in deps:
                return None
            if dep_app is None:
                dep_app = db_manager.get_app_by_key(app_key)
            if dep_app is None or dep_app.revoked:
                return []
            return bundle_ensure.shared_tenant_licences(db_manager, tenant_uuid, dep_app.id)

        containing: list[tuple[Any, list[License]]] = []
        for b in held:
            resolved = _resolve(b)
            if resolved is not None:
                containing.append((b, resolved))
        if not containing:
            raise _refuse(
                status.HTTP_404_NOT_FOUND,
                "app_not_in_bundle",
                "app_not_in_any_held_bundle",
                bundles=[str(b.key) for b in held],
            )
        if len(containing) > 1:
            # Several held bundles reach `app`. 059 DEDUPE: when they all resolve to
            # the SAME single licence (a dependency shared by conveyor and w2v, or a
            # licence one bundle holds and another merely needs), that licence is
            # the answer — no 409. Only genuinely different licences (or none) are
            # ambiguous.
            distinct = {str(lic.id) for _, lics in containing for lic in lics}
            if len(distinct) != 1:
                raise _refuse(
                    status.HTTP_409_CONFLICT,
                    "ambiguous_bundle",
                    "app_in_several_held_bundles",
                    bundles=[str(b.key) for b, _ in containing],
                )
            containing = [next((b, lics) for b, lics in containing if lics)]
        held_bundle, licences = containing[0]

        if not licences:
            raise _refuse(
                status.HTTP_404_NOT_FOUND,
                "no_app_licence",
                "no_active_per_app_licence_in_bundle",
                bundle=str(held_bundle.key),
            )
        if len(licences) > 1:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "ambiguous_app_licence",
                "several_per_app_licences_in_bundle",
                bundle=str(held_bundle.key),
                license_ids=[str(lic.id) for lic in licences],
            )
        license = licences[0]

        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        if license.expires_at is not None and license.expires_at < expires_at:
            expires_at = license.expires_at
        token, raw_token = db_manager.create_token(
            license_id=cast(UUID, license.id),
            token_type=TokenType.api.value,
            expires_at=expires_at,
        )
        logger.warning(
            "bundle_token_minted actor_sub=%s actor=%s actor_azp=%s tenant_id=%s bundle=%s "
            "app=%s license_id=%s token_id=%s expires_at=%s",
            user.sub,
            user.preferred_username,
            user.azp,
            tenant_uuid,
            held_bundle.key,
            app_key,
            license.id,
            token.id,
            expires_at.isoformat(),
        )
        return BundleTokenResponse(
            token=raw_token,
            expires_at=expires_at.isoformat(),
            tenant_id=tenant_uuid,
            bundle=str(held_bundle.key),
            app=app_key,
            license_id=str(license.id),
            token_id=str(token.id),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to mint bundle token",
            e,
            "Failed to mint bundle token",
        ) from e


class BundleEnsureRequest(BaseModel):
    """Body of POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure (PLAN §2.2)."""

    plans: list[str] = Field(
        default_factory=lambda: [bundle_ensure.DEFAULT_PLAN_CODE],
        min_length=1,
        description=(
            "Ordered plan-code preference for INSERTED licences: each app gets the first "
            "code that exists (non-revoked) on it, e.g. ['standard','perpetual']."
        ),
    )
    quota_override: int | None = Field(
        None,
        ge=0,
        le=bundle_ensure.MAX_OVERRIDE,
        description=(
            "Requests/month for each INSERTED licence instead of the plan's quota_monthly "
            "(stored on the licence, migration 041). Never applied to exists/adopt."
        ),
    )
    token_budget_override: int | None = Field(
        None,
        ge=0,
        le=bundle_ensure.MAX_OVERRIDE,
        description=(
            "Tokens/month for each INSERTED licence instead of the plan's budget. 422 for a "
            "tenant whose budget_currency is set: its budget is money and no token budget "
            "is ever pushed for it."
        ),
    )
    dry_run: bool = Field(False, description="Decide and report; write nothing.")

    @field_validator("plans")
    @classmethod
    def _plans_non_empty_unique(cls, v: list[str]) -> list[str]:
        codes = [c.strip() for c in v]
        if any(not c for c in codes):
            raise ValueError("empty plan code")
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate plan code")
        return codes


class BundleEnsureApp(BaseModel):
    decision: Literal["exists", "adopt", "insert"]
    license_id: str | None = Field(
        None, description="The (tenant, app) licence; null only for an insert on a dry run."
    )
    plan: str | None = Field(None, description="Plan code of that licence (or to be inserted).")
    metering: str = Field(
        ...,
        description=(
            "'quota_only' (budget_currency set: no token budget, ever), "
            "'token_and_quota' (budget_currency null), or 'pending' (an insert whose "
            "billing push did not complete; the nightly reconcile repairs it)."
        ),
    )
    dependency: bool = Field(
        False,
        description=(
            "Migration 059: true = not sold by this bundle but REQUIRED by one of its "
            "apps (app_requires closure). Its licence is the tenant's ONE licence for "
            "the app, shared across bundles; an inserted one is a dependency licence "
            "(internal-only: bundle-token, never a user token)."
        ),
    )


class BundleEnsureRefusal(BaseModel):
    app: str
    reason: str = Field(
        ...,
        description=(
            "several_candidates | member_licence | no_plan | other_bundle | "
            "app_key_too_long | lost_race. Nothing was written for this app."
        ),
    )


class BundleEnsureResponse(BaseModel):
    tenant_id: str
    bundle: str
    budget_currency: str | None
    apps: dict[str, BundleEnsureApp]
    refused: list[BundleEnsureRefusal]


_ENSURE_ERROR_STATUS = {
    "tenant_not_found": status.HTTP_404_NOT_FOUND,
    "bundle_not_found": status.HTTP_404_NOT_FOUND,
    "bundle_empty": status.HTTP_404_NOT_FOUND,
    "tenant_archived": status.HTTP_409_CONFLICT,
    "token_budget_for_money_tenant": status.HTTP_422_UNPROCESSABLE_ENTITY,
    # app_requires holds a cycle reachable from the bundle (migration 059).
    "dependency_cycle": status.HTTP_409_CONFLICT,
    # Another ensure for this tenant held the tenant lock past its timeout (retry).
    "ensure_in_progress": status.HTTP_409_CONFLICT,
}


@app.post(
    "/internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure",
    response_model=BundleEnsureResponse,
    operation_id="internal_ensure_tenant_bundle",
    tags=["internal"],
    responses={
        **COMMON_ERROR_RESPONSES,
        403: {
            "model": ErrorResponse,
            "description": (
                "A missing role; `unknown_api_client`; `tenant_owner_only` (a grantee); "
                "`bundle_not_ensurable_by_caller` (the caller's owner is not a "
                "bundle_ensurer of this bundle, migration 052). Nothing written."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "`tenant_not_found`, `bundle_not_found` or `bundle_empty`.",
        },
        409: {
            "model": BundleEnsureResponse,
            "description": (
                "At least one app was REFUSED (`refused` lists them; the other apps were "
                "still ensured — each app is independent and the call is idempotent). "
                "Also 409 `{detail:{error:'tenant_archived'}}` for an archived tenant, "
                "with nothing written, and 409 `{detail:{error:'ensure_in_progress'}}` "
                "when another ensure for the same tenant is still running (retry), and "
                "409 `{detail:{error:'dependency_cycle'}}` when app_requires holds a cycle "
                "reachable from the bundle (migration 059; nothing written)."
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "`token_budget_for_money_tenant` — `token_budget_override` sent for a "
                "tenant whose budget_currency is set. Nothing written."
            ),
        },
    },
)
async def internal_ensure_tenant_bundle(
    tenant_id: str,
    bundle_key: str,
    body: BundleEnsureRequest,
    user: User = Depends(require_bundle_ensurer),
):
    """Ensure the tenant holds one per-app licence for every app in ``bundle_key``.

    The HTTP form of ``scripts/provision_tenant_bundle.py``, running the SAME decision
    (``services.bundle_ensure``): ``exists`` / ``adopt`` (a sole tenant-level unbundled
    licence; a member's licence is never adopted) / ``insert`` / REFUSE. Inserted
    licences are metered through ``provision_license_metering``: the quota always,
    the token budget only when the tenant's ``budget_currency`` is null. A second
    call answers ``exists`` for every app and writes nothing.

    ROLES (``require_bundle_ensurer``): ``bundle-provisioner`` or an owner-write role
    (``license-admin`` / ``license-manager``). ``service-internal``,
    ``bundle-token-minter`` and ``license-reader`` are 403. It mints NO token; the
    bundle-token route does that, under its own role.

    ⛔ ENSURER ALLOWLIST (migration 052), NOT ``app_issuer``. The caller's owner
    must be a ``bundle_ensurer`` of the bundle, else 403
    ``bundle_not_ensurable_by_caller`` with nothing written (break-glass bypasses,
    audited). Without it any owner could ensure ``commonpractice`` on its own tenant
    and have a CommonPractice licence minted. Requiring caller ∈ app_issuer(app)
    instead would make every cross-app bundle impossible (merqi -> ``w2v``;
    commonpractice -> ``non-agentic-ai`` / ``n8n-nodes``), so the sanctioned
    cross-app flows are listed per bundle. Licences itself is the minter
    (tenant-level subject ``svc:<tenant-uuid>:<app>`` in realm ``service``, plan =
    the first code of ``plans`` that exists on the app), and each bundled app's
    issuer is GRANTED on the tenant (least privilege, ``models.bundle_grantees``).
    Direct minting (``POST /licenses/user``) keeps the issuer rule.

    ⛔ OWNER ONLY (migration 050): the caller's app must OWN the tenant (or be the
    break-glass client). This is how an owner sanctions the bundled apps' issuers
    on its tenant: each bundled app's issuing owners are GRANTED access in the
    same transaction as the bundle hold, so merqi ensuring the ``w2v`` bundle lets
    w2v mint ``w2v`` there. Another app's tenant reads as absent (404
    ``tenant_not_found``); a grantee gets 403 ``tenant_owner_only``.
    """
    tid = validate_tenant_uuid(tenant_id, source="path tenant_id")
    try:
        tenant = await asyncio.to_thread(
            _resolve_owned_tenant, user, tid, f"bundle_ensure:{bundle_key}"
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "tenant_not_found",
                    "tenant_id": tid,
                    "bundle": bundle_key,
                    "message": f"tenant {tid} is not in tenants",
                },
            ) from exc
        raise
    await asyncio.to_thread(_authorize_bundle_ensurer, user, tenant, bundle_key)
    try:
        # ASYNC, not ``def``: inserted licences are metered by awaiting
        # ``provision_license_metering`` on THIS (uvicorn's) loop. A ``def`` route
        # ran ``asyncio.run`` on a worker thread — a second loop sharing the token
        # provider's ``asyncio.Lock``, which then refused every later contended
        # refresh on the main loop ("bound to a different event loop"). The blocking
        # DB work runs in a worker thread inside ``plan_and_apply_async``.
        ep = await bundle_ensure.plan_and_apply_async(
            db_manager,
            tid,
            bundle_key,
            body.plans,
            body.quota_override,
            not body.dry_run,
            token_budget_override=body.token_budget_override,
            provisioned_by="POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure",
            granted_by=f"bundle:{bundle_key} by {user.realm}/{user.azp}",
        )
    # ClientSafeError, NOT a wider catch: ``EnsureError`` is a ClientSafeError whose
    # message is authored by services.bundle_ensure from the caller's own path
    # values (tenant id, bundle key), so echoing it is safe. Anything else falls to
    # the generic 500 below with no text echoed.
    except ClientSafeError as e:
        code = getattr(e, "code", "refused")
        logger.warning(
            "bundle_ensure_refused actor_sub=%s actor_azp=%s tenant_id=%s bundle=%s error=%s",
            user.sub,
            user.azp,
            tid,
            bundle_key,
            code,
        )
        raise HTTPException(
            status_code=_ENSURE_ERROR_STATUS.get(code, status.HTTP_409_CONFLICT),
            detail={"error": code, "tenant_id": tid, "bundle": bundle_key, "message": str(e)},
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to ensure tenant bundle",
            e,
            "Failed to ensure tenant bundle",
        ) from e

    def _plan_codes(plan_ids: set[UUID]) -> dict[UUID, str | None]:
        out: dict[UUID, str | None] = {}
        for plan_id in plan_ids:
            pl = db_manager.get_plan(plan_id)
            out[plan_id] = str(pl.code) if pl is not None else None
        return out

    # Blocking DB reads: off the event loop.
    plan_codes = await asyncio.to_thread(
        _plan_codes,
        {
            p.existing_plan_id
            for p in ep.apps
            if p.action in ("exists", "adopt") and p.existing_plan_id is not None
        },
    )

    def _plan_code(plan_id: UUID | None) -> str | None:
        return None if plan_id is None else plan_codes.get(plan_id)

    base_metering = "quota_only" if ep.budget_currency is not None else "token_and_quota"
    apps: dict[str, BundleEnsureApp] = {}
    refused: list[BundleEnsureRefusal] = []
    for p in ep.apps:
        if p.action == "REFUSE":
            refused.append(BundleEnsureRefusal(app=p.app_key, reason=p.refuse_reason))
            continue
        apps[p.app_key] = BundleEnsureApp(
            decision=cast(Literal["exists", "adopt", "insert"], p.action),
            license_id=p.license_id or None,
            plan=p.plan_code if p.action == "insert" else _plan_code(p.existing_plan_id),
            metering="pending" if p.metering_pending else base_metering,
            dependency=p.dependency,
        )
    result = BundleEnsureResponse(
        tenant_id=ep.tenant_id,
        bundle=ep.bundle_key,
        budget_currency=ep.budget_currency,
        apps=apps,
        refused=refused,
    )
    logger.warning(
        "bundle_ensured actor_sub=%s actor_azp=%s tenant_id=%s bundle=%s dry_run=%s "
        "decisions=%s refused=%s unmetered=%d grants_created=%s",
        user.sub,
        user.azp,
        tid,
        bundle_key,
        body.dry_run,
        {a: e.decision for a, e in apps.items()},
        [(r.app, r.reason) for r in refused],
        len(ep.unmetered),
        ep.grants_created,
    )
    if refused:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=result.model_dump())
    return result


class InternalSubjectResponse(BaseModel):
    """Subject identity, including the tenant it BELONGS TO (migration 029)."""

    id: str
    keycloak_sub: str
    keycloak_realm: str
    # ⚠️ NULL is a real, meaningful answer here and is NOT an error: it means
    # nobody has stated this subject's tenant yet. 029 adds the column without a
    # backfill, so on the day this ships EVERY subject reads null. A consumer must
    # treat null as "undecided" and must NOT fall back to inferring a parent from
    # the subject's licence rows — that join returns a SET (a subject may hold
    # licences under several tenants) and picking one silently is the inverted
    # inference this column exists to replace.
    tenant_id: str | None
    created_at: str | None


@app.get(
    "/internal/subjects/{subject_id}",
    response_model=InternalSubjectResponse,
    operation_id="internal_get_subject",
    tags=["internal"],
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {
            "model": ErrorResponse,
            "description": (
                "No subject resolves from `subject_id` — neither as a Keycloak `sub` "
                "claim nor as a local Subject.id UUID."
            ),
        },
    },
)
def internal_get_subject(
    subject_id: str,
    _user: User = Depends(require_internal_service),
):
    """
    Read one subject, including `tenant_id` — which tenant this subject belongs to.

    THE READER FOR migration 029. `subject.tenant_id` is the org half of the
    identity: before it, this registry could say "(tenant, app, subject) holds a
    licence" but could not say "this subject is a MEMBER of this tenant". Nothing
    else in the API exposes a `subject` row at all, so without this route the new
    column would be unreadable outside the database.

    `subject_id` resolves against BOTH identifiers, in the same order and for the
    same reason as `get_active_license` and `list_user_licenses`:
      1. `Subject.keycloak_sub` — the Keycloak `sub` claim, the only form
         license-authz ever holds, so the form a real caller sends;
      2. `Subject.id` — the locally minted uuid7 primary key.
    Both are UUID-shaped and indistinguishable by inspection, so resolving only
    one form would 404 real callers.

    ⚠️ `tenant_id` IS NULL FOR MOST SUBJECTS, and null means UNDECIDED, never "no
    tenant" — a caller that reads null as "belongs to nobody" is reading a fact
    this database has not stated. ⚠️ CORRECTED 2026-09-14: this docstring used to
    say null for EVERY subject, which was true only between 029 and 032. Measured
    on prod 2026-09-14: 51 of 264 stated, 213 null — 155 of those have never held
    a licence at all, 42 hold one only under a machine (`kind='service'`) tenant
    and 8 span more than one tenant, none of which this database can resolve. See
    `docs/svc-tenant-migration-plan.md` for what fills the rest and in what order,
    and `DatabaseManager.subject_tenant_parentage_partition` for the live counts.

    INTERNAL, not reader-visible, deliberately. It is guarded by
    `require_internal_service` (service-internal / onboarding-internal /
    license-admin) rather than the broader `require_internal_or_reader`, because
    an unscoped subject lookup is a subject-existence oracle: it takes no tenant
    scope, so a `license-reader` could probe whether an arbitrary Keycloak `sub`
    is known to the platform. Service accounts already hold that knowledge.
    """
    try:
        subject = db_manager.get_subject_by_keycloak_sub(subject_id)
        if not subject:
            try:
                subject = db_manager.get_subject(UUID(subject_id))
            # A non-UUID string that matched no keycloak_sub is simply "not found",
            # not a 422 — the parameter is deliberately polymorphic.
            except ValueError, AttributeError, TypeError:
                subject = None

        if not subject:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

        # Built from to_dict() rather than by hand so this route cannot drift from
        # the ORM's own rendering — to_dict already str()s the ids and preserves
        # None for an unstated tenant. `response_model` still validates the shape.
        return InternalSubjectResponse(**subject.to_dict())

    except HTTPException:
        raise
    # Top-level handler: spans subject lookup + response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to read subject",
            e,
            "Failed to read subject",
        ) from e


@app.get(
    "/internal/licenses/{license_id}",
    response_model=InternalLicenseResponse,
    operation_id="internal_get_license",
    tags=["internal"],
    responses=COMMON_ERROR_RESPONSES,
)
def internal_get_license(
    license_id: str,
    _user: User = Depends(require_internal_service),
):
    """
    Get a single license with enriched app and plan data.

    For internal service consumers.
    Requires a service-account JWT carrying one of this service's
    INTERNAL_SERVICE_ROLES: service-internal, onboarding-internal or
    license-admin. ⚠️ CORRECTED 2026-08-10 — `metering-internal` was rotated to
    `service-internal` and retired here; it is refused with 403.
    """
    try:
        lic = db_manager.get_license(UUID(license_id))
        if not lic:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="License not found")

        data: dict[str, Any] = {
            "id": str(lic.id),
            # UUID column (migration 016) → str() for the `str` response field
            # (inline dict, not via License.to_dict). See internal_list_licenses.
            "tenant_id": str(lic.tenant_id),
            "subject_id": str(lic.subject_id),
            "status": lic.status,
            # The SECOND constructor of this response model. It must carry
            # `effective_status` too — the field is required, and a single-licence
            # read that omitted the time-aware verdict while the list carried it
            # would put the two internal reads of the SAME row back into
            # disagreement, one level down from the defect that added it.
            "effective_status": lic.effective_status(),
            "starts_at": lic.starts_at.isoformat() if lic.starts_at else None,
            "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
            "metadata": lic.license_metadata,
            # migration 041: same per-licence overrides as internal_list_licenses.
            "token_budget_override": lic.token_budget_override,
            "quota_override": lic.quota_override,
        }
        if lic.app:
            data["app"] = AppInfo(
                id=str(lic.app.id),
                key=lic.app.key,
                name=lic.app.name,
            )
        if lic.plan:
            data["plan"] = PlanInfo(
                id=str(lic.plan.id),
                code=lic.plan.code,
                name=lic.plan.name,
                rate_limit_rpm=lic.plan.rate_limit_rpm,
                rate_limit_burst=lic.plan.rate_limit_burst,
                quota_monthly=lic.plan.quota_monthly,
                token_budget_monthly=lic.plan.token_budget_monthly,
            )

        return InternalLicenseResponse(**data)

    except ValueError as e:
        raise _client_error(
            status.HTTP_400_BAD_REQUEST, "Invalid UUID", e, "internal_get_license: Invalid UUID"
        ) from e
    except HTTPException:
        raise
    # Top-level handler: spans UUID parse + DB + enriched response model build; any unexpected error returns 500.
    except Exception as e:
        raise _internal_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to get internal license",
            e,
            "Failed to get internal license",
        ) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",  # nosec B104
        port=8544,
        reload=True,
        ssl_keyfile="/certs/client-key.pem",
        ssl_certfile="/certs/client-cert.pem",
    )
