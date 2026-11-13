# Licenses

[![CI](https://github.com/nonagenticai/licenses/actions/workflows/ci-public.yml/badge.svg)](https://github.com/nonagenticai/licenses/actions/workflows/ci-public.yml)
[![Release](https://img.shields.io/github/v/release/nonagenticai/licenses)](https://github.com/nonagenticai/licenses/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A licensing service that manages application entitlements, plans, and API tokens for the nonagentic.ai platform. Port 8544.

| | |
|---|---|
| **Stack** | Python 3.14 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 |
| **Database** | PostgreSQL 18 via PgBouncer (transaction mode) |
| **Runs on** | Kubernetes (any cluster — no cloud or managed-service dependency) |

## What Licenses Does

When a user authenticates through Keycloak at portal.nonagentic.ai, the portal provisions a license token tied to a specific app and plan. Every subsequent API call from an n8n custom node or platform service carries that token in an Authorization header. Istio's ext_authz filter intercepts each inbound request at the IngressGateway and calls this service's `/license/introspect` endpoint to validate the token. The introspection response tells the gateway whether the request is allowed and which tenant it belongs to, so the gateway can inject `X-Tenant-Id`, `X-License-Id`, `X-App-Id`, and `X-Plan-Id` headers before traffic reaches the downstream service.

Behind the introspection endpoint, the service manages a hierarchy of domain objects. Apps represent licensable modules (one per n8n custom node package). Plans define per-app tiers such as test, trial, and full, optionally scoped to a geographic region and carrying rate-limit and quota fields that Envoy can enforce. Licenses bind a Keycloak subject to an app under a specific plan, with status tracking (active, revoked, expired, pending) and configurable expiration. Tokens are SHA-256 hashed API keys bound to licenses -- the raw token is returned exactly once at creation time and never stored, so introspection works by hashing the incoming token and looking it up by hash.

No PII is stored anywhere in this service. Identity is handled entirely by Keycloak -- the only user reference kept is the opaque `keycloak_sub` UUID in the Subject table. All primary keys use UUIDv7, which embeds a timestamp for natural ordering. The service exposes CRUD endpoints for apps, plans, licenses, regions, and tokens, all tenant-scoped and protected by a role-based access control system with three tiers: license-admin (full CRUD), license-manager (create and revoke), and license-reader (read-only plus token regeneration).

The service is stateless in its application tier — all state lives in PostgreSQL via PgBouncer. It runs as a Kubernetes Deployment behind a ClusterIP Service on port 8544, so it is reachable only from inside the cluster (or wherever your gateway puts it). The only publicly callable endpoint is `/license/introspect`, which requires no authentication because the token itself is the credential being validated.

## Features

- **Token introspection for Istio ext_authz** — validates opaque license tokens and returns tenant context headers so every service in the mesh gets multi-tenant isolation without implementing its own auth
- **Hierarchical licensing model** — organizes Apps, Plans, Licenses, and Tokens in a clear hierarchy so entitlements can be managed at any level of granularity
- **Region-scoped plans** — allows plans to be tied to geographic regions with unique constraints on (app, code, region) so pricing and rate limits can vary by locale
- **Rate limit and quota fields** — stores per-plan `rate_limit_rpm`, `rate_limit_burst`, and `quota_monthly` values so Envoy can enforce traffic shaping at the mesh layer
- **SHA-256 token hashing** — stores only the hash of API tokens so a database breach never exposes raw credentials
- **Role-based access control** — enforces three permission tiers (admin, manager, reader) extracted from JWT realm roles or ext_authz headers so API access is scoped appropriately
- **Zero PII storage** — references Keycloak subjects by opaque UUID only, keeping all personal data in the identity provider where it belongs
- **UUIDv7 primary keys** — provides timestamp-ordered identifiers across all tables so queries naturally sort by creation time without extra indexes
- **Automatic token expiration** — supports configurable `expires_days` per token type so stale credentials age out without manual intervention
- **Last-used tracking** — updates `last_used_at` on each successful introspection so operators can identify and clean up dormant tokens

## Authentication

Auth enabled by default (`ENABLE_AUTH=true`). The service runs behind Istio mTLS in production.

When enabled, requests to protected prefixes (`/licenses`, `/apps`, `/plans`, `/regions`, `/license/revoke`, `/internal`, `/tenants`, `/offerings` — see `PROTECTED_PREFIXES` in `auth/middleware.py`) are authenticated in priority order:

| Priority | Method | Details |
|----------|--------|---------|
| 1 | JWT Bearer token | **Cryptographically verified here** (`auth/jwt_verify.py`): RS256 signature against the issuer's JWKS, expiry, and `iss` pinned to an allowlist. Roles come from the *verified* `realm_access.roles`. A token shaped like a JWT that fails verification is a hard 401 and never falls through. |
| 2 | Opaque license token | **Always introspected** via the local `/license/introspect` endpoint. Identity and tenant come from the introspection result, never from client headers. Grants `license-reader` only (`LICENSE_TOKEN_ROLES`). |
| 3 | No Bearer token | **401, fails closed.** There is no header-only fallback user. |

`X-Tenant-Id` does not authenticate anything. It may *retarget* a different tenant only for a `license-admin` or a verified trusted-proxy `azp`; a header contradicting a verified claim is a 403, and a claim-less non-proxy caller's header is ignored.

> ⚠️ This table previously stated that the JWT was "decoded without signature verification (trusts upstream ext_authz)", and described two header-trust fallbacks. All of that describes holes that were **closed** (2026-07-20, 2026-07-23 and Stage 2). The premise was false even when written: `licenses.nonagentic.ai` is not in the ext_authz `AUTH_REQUIRED_HOSTS` allowlist, and the NetworkPolicy admits the whole pod CIDR, so any in-mesh caller reaches this middleware directly. See the module docstring of `auth/jwt_verify.py`, which exists because of that.

Health endpoints (`/health`, `/health/*`), docs (`/docs`, `/redoc`, `/openapi.json`), and `/license/introspect` are always exempt.

**Also exempt, and easy to miss:** `GET /tenants/{name_or_id}/byo-mode` (which takes an optional `?app=`). It is the *only* one — `_BYO_MODE_OPEN_ROUTES` in `auth/middleware.py` also matches `GET /tenants/{id}/apps/{key}/byo-mode`, but **no GET is registered on that path** (it serves PUT and DELETE only), so that second alternative is currently dead. `worker_management` calls them on the trusted network with no token, so priority 3 above ("no Bearer → 401") does **not** hold for that route even though `/tenants` is a protected prefix. The exemption matches the exact route shapes — a bare `endswith("/byo-mode")` would auth-exempt any path ending that way — and the PATCH/PUT/DELETE variants keep their `license-admin` dependency.

### Roles

| Role | Permissions |
|------|-------------|
| `license-admin` | Full CRUD on all resources (apps, plans, licenses, regions, tokens, revocation) |
| `license-manager` | Create licenses, create tokens, revoke tokens/licenses |
| `license-reader` | Read-only access to licenses, apps, plans, regions. **Cannot** regenerate tokens — that is a destructive credential rotation and has required a write role since 2026-07-27. |

## Quick Start

```bash
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8544
uv run pytest tests/ -v
uv run ruff check main.py models.py auth/ tests/ && uv run ruff format --check main.py models.py auth/ tests/
```

## API Endpoints

### Token Introspection `/license`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/license/introspect` | Validate token, return license/app/plan info and tenant_id (no auth required) |
| POST | `/license/revoke` | Revoke a specific token or all tokens for a license |

### Licenses `/licenses`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/licenses` | List licenses with filters (status, app_id, plan_id, subject_id), scoped by tenant_id |
| GET | `/licenses/user/{subject_id}` | List all licenses for a subject (keycloak_sub or internal UUID) |
| POST | `/licenses/user/{subject_id}` | Create a new license for a subject |
| POST | `/licenses/{license_id}/tokens` | Create a new token for a license |
| POST | `/licenses/{license_id}/regenerate-token` | Revoke all existing tokens and issue a new one, inheriting their expiry. 409 if the license is not active, or if every unrevoked token has already expired (nothing is revoked in either case) |

### Apps `/apps`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/apps` | List all non-revoked apps |
| GET | `/apps/{app_id}` | Get app by UUID or key |
| POST | `/apps` | Create a new app |
| GET | `/apps/{app_id}/plans` | List plans for an app |
| GET | `/apps/{app_id}/licenses` | List licenses for an app |

### Plans `/plans`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/plans` | List plans with optional app_id and region_id filters |
| GET | `/plans/{plan_id}` | Get plan by UUID |
| POST | `/plans` | Create a new plan |

### Regions `/regions`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/regions` | List regions (active_only filter, defaults to true) |
| POST | `/regions` | Create a new region |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Full health check with DB status |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks DB connectivity) |

## Integrating: calling the licence service from another application

Any service that gates a feature on a licence calls **one HTTP endpoint**. There is no SDK to
vendor and nothing to copy into your tree.

```http
POST $LICENSE_INTROSPECT_URL/license/introspect
Content-Type: application/json

{"token": "<the opaque licence token your caller presented>"}
```

```json
{"active": true, "tenant_id": "...", "license_id": "...", "subject_id": "...",
 "subject_keycloak_sub": "...", "plan": "...", "byo_mode": false}
```

- **The token in the body is the credential.** The endpoint takes no API key or client secret; the
  deployment-level `INTROSPECT_REQUIRE_AUTH` flag can additionally require a bearer from
  non-loopback callers. Set `LICENSE_INTROSPECT_URL` from your own deployment config — never a
  literal hostname in source.
- **`active` is the only thing that grants.** `tenant_id` is the identity an ext_authz enforcer
  forwards downstream as `X-Tenant-Id`. `license_id`, `subject_id` and `subject_keycloak_sub`
  identify; they grant nothing.
- **A client-side check is not the control.** Plans, entitlements and RBAC are enforced here,
  server-side. A check copied into a consumer can be deleted by whoever runs it — treat it as UX,
  never as the thing that protects a paid feature.
- **Do not fork the middleware.** `auth/middleware.py` in this repository is the reference
  consumer for a Python service. A per-service copy drifts from the contract while looking
  identical; if it lacks behaviour you need, change it here.
- **Timeouts and failure policy are the caller's.** A licence-service blip must not cascade; decide
  in your service whether an unreachable introspection means "deny" and for how long.

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8544` | Server port |
| `DATABASE_URL` | **required — no default** | SQLAlchemy connection string. Unset ⇒ the service refuses to start (`RuntimeError` naming `DATABASE_URL`). In the cluster it comes from the `licenses-env` Secret. |
| `POSTGRES_SSLMODE` | `require` | SSL mode for PostgreSQL |
| `SERVICE_NAME` | `licensing-service` | Service identifier |
| `VERSION` | `1.0.0` | Application version |
| `ENVIRONMENT` | `production` | Environment name (set to `development` to auto-enable docs) |
| `ENABLE_DOCS` | `false` | Explicitly enable Swagger UI and ReDoc |
| `ENABLE_AUTH` | `true` | Enable/disable auth middleware entirely |
| `LOG_LEVEL` | `INFO` | Logging level |
| `REALM_NAME` | `secure-apps` | Keycloak realm name |
| `KEYCLOAK_URL` | `https://keycloak.example.com` | Keycloak server URL |
| `KEYCLOAK_REALM` | `secure-apps` | Keycloak realm |
| `SERVICE_CLIENT_ID` | *(empty)* | Client id of the **outbound** service account (`service_auth.py`, client-credentials grant) |
| `SERVICE_CLIENT_SECRET` | *(empty)* | Secret for that service account |
| `SERVICE_TOKEN_URL` | *(empty)* | Explicit token endpoint for the service account. Empty → derived from `KEYCLOAK_URL` + `KEYCLOAK_REALM`. Production sets it, because the SA client lives in `secure-apps` while `KEYCLOAK_REALM` points elsewhere |
| `KEYCLOAK_TRUSTED_ISSUERS` | *(empty)* | Comma-separated issuer allowlist for **inbound** JWT verification. Empty → derived as `KEYCLOAK_URL` + the `portal` and `secure-apps` realms (`auth/jwt_verify.py`) |

> **`KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`, `KEYCLOAK_SSL_VERIFY` and `CA_CERT_PATH` used to be listed here. No line of this service reads them.** Their only consumer was `auth/validator.py`, deleted in PR #140; the `auth/config.py` members behind them were deleted alongside this note. Production never set them either — the `licenses-env` secret holds exactly `DATABASE_URL`, `POSTGRES_SSLMODE`, `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `SERVICE_CLIENT_ID`, `SERVICE_CLIENT_SECRET`, `SERVICE_TOKEN_URL` (verified live 2026-08-17). They survive only in `docker-compose.yml` and `test-keycloak.sh`, which drive a **local** Keycloak stack and read them from the shell, so those two files still set them on purpose. `tests/unit/test_config_surface_is_consumed.py` now fails if unread configuration surface reappears.

## Architecture

```
main.py                            # FastAPI app, all routes, Pydantic request/response models
models.py                          # SQLAlchemy ORM: Region, Subject, App, Plan, License, Token + DatabaseManager
auth/
├── __init__.py                    # Public API exports
├── config.py                      # KeycloakSettings: Keycloak URL/realm, the OUTBOUND service-account credential, tenant-proxy allowlist. No part in inbound verification
├── dependencies.py                # FastAPI deps: get_current_user, require_role, require_any_role
├── middleware.py                   # AuthMiddleware (JWT signature/expiry/issuer VERIFICATION, opaque-token introspect)
├── jwt_verify.py                   # RS256 + JWKS + issuer allowlist — the verification itself
├── models.py                      # User Pydantic model with role checks
├── tenant.py                      # Tenant ID extraction from trusted headers or JWT sub
services/
└── __init__.py                    # Empty (reserved for future service extraction)
scripts/                           # 22 operator scripts, NOT the 4 once listed here. Four groups:
├── apply_migrations.py            #   migrations — apply_migrations, build_schema_arms, dump_schema
├── seed_*.py / provision_*.py     #   seeding + tenant/token provisioning (fr, kag, w2v, portal, n8n-dev)
├── audit_*.py / reconcile_*.py    #   reconciliation — orphan tenants, metering, stale licence expiry
└── pip-audit.sh, export-openapi.py, setup-dev.sh, openapi_env.py
tests/
├── conftest.py                    # Shared fixtures (test client, DB mocks)
├── unit/                          # 51 unit test modules
├── integration/                   # 6 integration test modules (marked, excluded by default)
├── auth/                          # 7 auth-specific test modules
└── contract/                      # Schemathesis hooks — NO test files yet; nothing in CI invokes them
```

## Data Models

All tables use UUIDv7 primary keys.

| Model | Key Fields | Description |
|-------|-----------|-------------|
| **Region** | code (unique), name, active | Geographic region for scoping plans and licenses |
| **Subject** | keycloak_sub (unique), keycloak_realm | Opaque reference to a Keycloak user, no PII stored |
| **App** | key (unique), name, description, revoked | A licensable application or module |
| **Plan** | app_id, code, name, region_id, rate_limit_rpm, quota_monthly | Per-app tier, unique on (app_id, code, region_id) |
| **License** | subject_id, app_id, plan_id, tenant_id, status, expires_at | A subject's entitlement with status lifecycle |
| **Token** | license_id, token_hash (SHA-256), token_type, expires_at, last_used_at | API token bound to a license, raw token never stored |

## Testing

```bash
uv run pytest tests/ -v   # Unit + auth tests (excludes integration by default)
uv run pytest tests/unit/ -v                                   # Unit tests only
uv run pytest tests/integration/ -v -m integration             # Integration tests (TestClient + Postgres; live cluster only with LICENSES_URL)
uv run pytest tests/auth/ -v                                   # Auth middleware and RBAC tests
uv run ruff check main.py models.py auth/ tests/              # Lint
uv run ruff format --check main.py models.py auth/ tests/     # Format check
```

**Test database** (`tests/support/ephemeral_postgres.py`): `TEST_DATABASE_URL` →
`DATABASE_URL` → otherwise a throwaway `postgres:18` container (tmpfs, random
`127.0.0.1` port) is started for the session and removed by its own id afterwards,
pass or fail. No docker and no URL ⇒ the run exits with a message. There is **no**
`localhost:5432`/`5447` default — never point tests at a host Postgres.

Coverage threshold: **42%** (`fail_under` in `pyproject.toml`). This line claimed 80% while the
gate was set to 42 — read the value from `pyproject.toml`, not from here.

## Provisioning the demo tenant

`scripts/provision_demo_tenant.py` registers the `demo` tenant
(`0195f48b-d000-7000-8000-000000000103`, `kind=test`, `cost_centre=tests`, realm
`portal`, `byo_mode` copied from `n8n-dev` at run time) and mints one licence + token
per app for the `n8n-demo` user. It never touches a token outside the demo tenant.

The DB URL exists only inside the pod, so run it there (needs cluster-admin).
**Dry run is the default**: without `--apply` it prints the plan and writes nothing, and
`--rotate-demo` revokes nothing. `--dry-run` is accepted as a no-op alias.

```bash
kubectl -n licenses exec deploy/licenses -- python scripts/provision_demo_tenant.py \
  --owner-sub <keycloak-sub-of-n8n-demo> --tenant-owner platform --apps kanban,kag,coding

kubectl -n licenses exec deploy/licenses -- python scripts/provision_demo_tenant.py \
  --owner-sub <keycloak-sub-of-n8n-demo> --tenant-owner platform --apps kanban,kag,coding --apply \
  [--monthly-token-budget 2000000] > demo-tokens.json
```

- **Output:** STDOUT is only the token JSON, printed once after the commit. The plan,
  the fingerprints and progress go to STDERR. Tokens are never logged; treat
  `demo-tokens.json` as a secret.
- **The tenant is fixed.** The id and the name `demo` are constants. `--tenant-id` and
  `--tenant-name` do not exist, so the script cannot be pointed at a real tenant.
- **Guards:** exits 2 with nothing written if any tenant with the demo id OR the name
  `demo` exists and is not exactly `name=demo, kind=test, cost_centre=tests`, or if it
  is archived. Exits 1 if an app, plan or `n8n-dev` is missing. Everything runs in one
  REPEATABLE READ transaction.
- **Fingerprint:** covers ALL tokens and licences in the database (`count`,
  `max(created_at)`, row-state md5). It is taken before and after the writes, excluding
  only the exact row ids this run inserted or updated. Any other difference rolls the
  transaction back (exit 3).
- **Re-run:** reuses the tenant and skips apps that already have an active demo
  licence. `--rotate-demo` mints a new token and revokes only the tokens this script
  minted on that licence. Those token ids are recorded in the licence's
  `metadata.provision_demo_tenant_token_ids`, and a licence without the
  `provisioned_by=provision_demo_tenant` marker is refused. There are two ownership
  barriers. First, the plan refuses the whole run (exit 1) if any recorded id belongs
  to another licence. Second, the revoke UPDATE is scoped to that licence and the demo
  tenant, and its RETURNING set must match the plan (exit 3 otherwise).
- **Tenant row lock:** the demo tenant row is read `FOR SHARE` as the transaction's
  first statement, and re-checked `FOR SHARE` right before commit. A concurrent
  reclassification either blocks until the run ends, or aborts the run (exit 4).
- **Budget:** `--monthly-token-budget N` licenses each app under a per-app `demo`
  plan carrying `token_budget_monthly=N`. The script refuses rather than changes an
  existing `demo` plan that has a different budget. It does not call billing: the
  scheduled metering-reconcile sweep pushes the budget.

## CI/CD

`.github/workflows/ci-public.yml` — self-contained, GitHub-hosted runners, no repository secrets:

```
secret scan (betterleaks)  →  private-refs guard  →  ruff + ty + pytest  →  pip-audit
```

The tests run against a throwaway Postgres 18 started by the workflow. Image builds are opt-in
per repository (the `PUBLISH_IMAGE` repository variable); when enabled, `main` publishes to
`ghcr.io/nonagenticai/licenses` tagged `sha-<commit>`, `main-<run>`, and `latest`.

## Docker

```bash
docker build -t licenses .
docker run -p 8544:8544 licenses
docker run -p 8544:8544 -e LOG_LEVEL=DEBUG -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/licenses -e ENABLE_AUTH=false -e ENABLE_DOCS=true licenses
```

## Dependencies

- **PostgreSQL 18** — persistent storage via PgBouncer (pgbouncer:6432)
- **Keycloak** — identity provider (`auth.nonagentic.ai`) for JWT validation and realm roles. It runs **in-cluster** (Deployment `keycloak/keycloak`, verified live 2026-08-17); the "VPS-only" this line used to carry has been wrong since the 2026-04-07 migration. The public hostname is fronted by Cloudflare, which is why JWKS fetches must send a browser `User-Agent` — see `auth/jwt_verify.py`
- **Istio ext_authz** — calls `/license/introspect` on every mesh request for token validation
- **FastAPI** — web framework
- **Uvicorn** — ASGI server
- **Pydantic** — settings and schema validation
- **SQLAlchemy 2.0** — sync ORM with psycopg3 driver
- **PyJWT** — JWT signature/expiry/issuer verification in the auth middleware (`auth/jwt_verify.py`: JWKS, `kid`-matched, issuer pinned against the trusted-issuer allowlist)

## Known Issues

- **Schemathesis contract tests never ran** — the previous wording ("temporarily disabled in CI, `continue-on-error` commented out") implied a step that exists and is switched off. There is no such step: `schemathesis` appears nowhere in `.github/workflows/`, and `tests/contract/` holds only `schemathesis_hooks.py` and `__init__.py` — no test files. The dependency is declared in `pyproject.toml`; nothing invokes it.
- **No src/ directory** — app code lives at repo root (`main.py`, `models.py`, `auth/`) rather than under `src/`. Ruff config targets these paths directly.
- **services/ package empty** — `services/__init__.py` exists but contains no code. Reserved for future extraction of business logic from `main.py`.

## Security

Report vulnerabilities privately through the repository's Security tab — see
[`SECURITY.md`](SECURITY.md). Do not open a public issue for a security problem.

## Contributing

Issues and pull requests are welcome. See
[`CONTRIBUTING.md`](https://github.com/nonagenticai/.github/blob/main/CONTRIBUTING.md) for the
organisation-wide rules; setup, test and lint commands are in this README. Commits carry DCO
sign-off.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
