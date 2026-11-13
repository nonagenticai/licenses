#!/usr/bin/env python3
"""
Idempotent baseline seed for fr-frontend + signal apps and their `standard`
plans. Runs against the licenses Postgres on each release; safe to re-run.

A fresh licenses database has neither these app rows nor their plans, so
fr-backend's super-admin user-provisioning surface 404s when it tries to
mint a license under `fr-frontend / standard`. This seed closes that gap
without touching unrelated data — every INSERT is gated on a SELECT, so
the script is a no-op once the rows exist.

This is also the file re-run after a DR restore, so an app key that is live
but undeclared here comes back as NOTHING. A key whose app enforces it
fail-closed (merqi `marketing` / `sales`) is then a tenant-wide outage with
no obvious cause. Declare a load-bearing key here even when a prod row
already exists ad-hoc — the SELECT gate makes that free.

Usage:
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \
        python scripts/seed_apps.py

In-cluster: ships as a k8s Job manifest in gitops-production.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, cast
from uuid import UUID

# Make `models` importable when run from anywhere in a checkout, or from inside
# the licenses container (working dir = /app).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import App, DatabaseManager, Plan, issuers_for_app_key  # noqa: E402

logger = logging.getLogger("seed_apps")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

# ---------------------------------------------------------------------------
# METERING ALLOWANCES — every seeded plan MUST carry both.
#
# ⚠️ NULL does NOT mean "not set yet". Per migration 017 the contract is
# NULL = UNLIMITED, 0 = block-all, >0 = monthly cap. So a plan seeded with NULLs
# is an explicitly UNCAPPED plan, and the whole metering chain treats it as
# such: the licences reconcile selects only licences "on a plan that OWES
# billing a quota row OR a token budget", billing therefore holds no
# token_budgets row, and every debit is skipped with
# {"event":"budget_debit_skipped","reason":"no_active_budgets"} at INFO — no
# ceiling, no alert. Measured 2026-08-11: 42 of 88 prod plan rows were
# unlimited, covering 43 of 141 live licences, and 5 of the last 7 licences
# minted were on one of them. George, 2026-08-11: "every plan needs to be
# metered! that's the whole point of having metering!"
#
# WHY THESE NUMBERS — measured, not guessed (billing_cost_events, 3 months):
#   * busiest single licence, current month ...... 11,379,133 tokens
#     (019f7f7c…, app w2v — and 9,428,668 of that landed in ONE day,
#      2026-08-10, ~40x its ~180k/day baseline)
#   * next busiest, 3-month peak ..................  7,282,765 tokens
#   * busiest single licence, requests/month ......      5,330
# 50M tokens is ~4.4x the observed peak and ~1.8x that licence's 31-day
# projected run-rate; 1M requests is ~188x its observed peak. Deliberately
# generous: the point of this change is that usage becomes MEASURED and
# enforceable, not that anyone is denied on day one. Tighten later with data.
#
# ⚠️ The fleet's older 10,000,000 plans are now the TIGHTER ones — one licence
# already peaked at 73% of that, and a w2v-style one-day spike would exceed it.
# Raising those is a separate change: the reconcile only CREATES missing
# budgets, so an existing budget's amount does not follow a plan edit.
DEFAULT_QUOTA_MONTHLY = 1_000_000
DEFAULT_TOKEN_BUDGET_MONTHLY = 50_000_000

# ---------------------------------------------------------------------------
# Baseline: apps + their `standard` plan. Add new entries here when a new
# app permanently joins the fleet — the seed stays idempotent.
# ---------------------------------------------------------------------------

APPS: list[dict[str, Any]] = [
    {
        # The fr PACKAGE entitlement, enforced per-tenant at fr-backend.
        # fr-backend looks this up via GET /licenses/active?tenant_id=&app_key=fr
        # (JWT-mode: the end user authenticates with a Keycloak JWT, not an
        # opaque token, so entitlement is resolved by tenant+app). One active
        # `fr` license per customer tenant lets every Keycloak user in that
        # tenant use fr; fr then drives kanban/n8n/kag/requirements on their
        # behalf over the trusted network (those need no per-user license).
        # Distinct from `fr-frontend` below, which is the legacy per-USER
        # super-admin provisioning surface — NOT the package gate.
        "key": "fr",
        "issuers": ("fr",),
        "name": "FR",
        "audience": "human",
        "description": "Foundation Reasoning package entitlement — per-tenant gate enforced at fr-backend (kanban/n8n/kag driven by the system).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the fr package.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        "key": "fr-frontend",
        "issuers": ("fr",),
        "name": "FR Frontend",
        "audience": "human",
        "description": "Foundation Reasoning frontend (Svelte) — provisions user licenses via super-admin.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-user plan for fr-frontend.",
            # Conservative defaults — operators can update later via the API.
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        "key": "signal",
        "issuers": ("platform", "fr"),
        "name": "Signal",
        "audience": "human",
        "description": "Signal messenger surface — paired with fr provisioning to give phone-bound users a license.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-user plan for the Signal surface.",
            "rate_limit_rpm": 60,
            "rate_limit_burst": 10,
            "duration_days": None,
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        "key": "commonpractice",
        "issuers": ("commonpractice",),
        "name": "Common Practice",
        "audience": "human",
        "description": "Common Practice platform — per-tenant license gated at the app layer (Spec Part 9, Gate 2).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for Common Practice.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    # strata-configurator (migration 067) is RETIRED by migration 084 (merged into
    # `agent`; live row revoked) and is deliberately not declared: a rebuilt
    # database has no such app.
    {
        # care-bus (the generic interaction layer), migration 067. Its own owner issues
        # its licences via secure-apps/care-bus-licenses-admin (owner care-bus, migration
        # 068) — commonpractice's pattern.
        "key": "care-bus",
        "issuers": issuers_for_app_key("care-bus"),
        "name": "Care Bus",
        "audience": "human",
        "description": "Care Bus — per-tenant licence for the interaction layer, issued by its own owner, migration 067.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for Care Bus.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # agent (agent.nonagentic.ai), migration 070. Per-tenant licence issued on
        # signup approval by secure-apps/agent-licenses-admin (owner agent) — the
        # strata apps' pattern. Issued by its own owner only.
        "key": "agent",
        "issuers": issuers_for_app_key("agent"),
        "name": "Agent",
        "audience": "human",
        "description": "Agent (agent.nonagentic.ai) — per-tenant licence issued by its own owner on signup approval, migration 070.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for Agent.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
        # Migration 071: agent issues on `byo` like CommonPractice (owner decision
        # 2026-10-20) — CP's `byo` row shape, limits copied from `standard`.
        # `standard` stays: licences already issued on it keep working.
        "extra_plans": (
            {
                "code": "byo",
                "name": "Bring your own key",
                "description": (
                    "The workspace brings its own provider key and pays the provider; "
                    "the platform invoices the markup."
                ),
                "rate_limit_rpm": 600,
                "rate_limit_burst": 60,
                "duration_days": None,  # perpetual
                "quota_monthly": DEFAULT_QUOTA_MONTHLY,
                "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
            },
        ),
    },
    {
        # The w2v (web-to-video) PACKAGE entitlement, gated per-tenant at
        # w2v_orchestrator. JWT-mode like fr/commonpractice: the SPA user
        # authenticates with a Keycloak JWT (no opaque token), so entitlement is
        # resolved by tenant+app via GET /licenses/active?tenant_id=&app_key=w2v
        # (w2v_orchestrator GET /api/license/status). One active `w2v` license per
        # customer tenant entitles every Keycloak user in that tenant.
        "key": "w2v",
        "issuers": ("w2v",),
        "name": "W2V",
        "audience": "human",
        "description": "Web-to-Video package entitlement — per-tenant gate enforced at w2v_orchestrator (JWT-mode).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the w2v package.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The webscraper / ingestion PRODUCT entitlement (P5 of the commercial
        # two-axis licensing model). ingestion_service resolves a tenant's
        # scraper posture + entitlement by tenant+app via
        # GET /tenants/{id}/byo-mode?app=ingestion and
        # GET /licenses/active?tenant_id=&app_key=ingestion, and emits scrape
        # cost_events under the ingestion service key so per-(tenant, product)
        # markup + surcharge apply. Seeding the App makes the product a
        # first-class licensable entity; enforcement + a priced Offering are
        # provisioned separately (SA-gated). This key MUST match the billing
        # service key + the modality/consumer path prefix, all "ingestion".
        "key": "ingestion",
        "issuers": ("platform", "fr"),
        "name": "Ingestion / Webscraper",
        "audience": "human",
        "description": "Webscraper + document-ingestion product — per-tenant gate + metered scraping.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the ingestion/webscraper product.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The KARMA entity-extraction stage as a metered SUB-PRODUCT (P6 of the
        # commercial two-axis licensing model). karma_service emits one
        # cost_event per completed enrichment under the "karma" service key so
        # per-(tenant, product) markup applies. Registering the App makes karma a
        # first-class metered entity a future KAG Offering can bundle its usage
        # policy onto — it does NOT gate access (karma is an internal KAG pipeline
        # stage; ACCESS entitlement is enforced at the customer-facing KAG surface,
        # never at an internal stage). This key MUST match the billing service key
        # + the consumer path prefix, both "karma".
        "key": "karma",
        "issuers": ("platform", "fr"),
        "name": "KARMA Enrichment",
        "audience": "platform",
        "description": "KARMA entity-extraction stage — metered per-product usage line for the KAG pipeline.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the KARMA enrichment product.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The HiRAG hierarchical-decomposition stage as a metered SUB-PRODUCT (P7
        # of the commercial two-axis licensing model). hirag_service emits one
        # cost_event per completed /ingest/hierarchical (= one document decomposed)
        # under the "hirag" service key so per-(tenant, product) markup applies.
        # Registering the App makes hirag a first-class metered entity a future KAG
        # Offering can bundle its usage policy onto — it does NOT gate access
        # (hirag is an internal KAG pipeline stage; ACCESS is enforced at the
        # customer-facing KAG surface). This key MUST match the billing service
        # key + the consumer path prefix, both "hirag".
        "key": "hirag",
        "issuers": ("platform", "fr"),
        "name": "HiRAG Decomposition",
        "audience": "platform",
        "description": "HiRAG hierarchical-decomposition stage — metered per-product usage line for the KAG pipeline.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the HiRAG decomposition product.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The KAG-graph build stage as a metered SUB-PRODUCT (P7 of the commercial
        # two-axis licensing model). kag_graph_service emits one cost_event per
        # completed /ingest/subgraph (= one document graph-built) under the
        # "kag-graph" service key so per-(tenant, product) markup applies.
        # Registering the App makes kag-graph a first-class metered entity a future
        # KAG Offering can bundle its usage policy onto — it does NOT gate access
        # (kag-graph is an internal KAG pipeline stage; ACCESS is enforced at the
        # customer-facing KAG surface). This key MUST match the billing service key
        # + the consumer path prefix, both "kag-graph".
        "key": "kag-graph",
        "issuers": ("platform", "fr"),
        "name": "KAG Graph Build",
        "audience": "platform",
        "description": "KAG-graph build stage — metered per-product usage line for the KAG pipeline.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the KAG-graph build product.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The MLX inference backend as a metered SUB-PRODUCT. mlx_backend runs on
        # Apple-silicon bare metal (mini2, launchd — no edge route, no
        # route-registry entry) and self-meters: it emits one cost_event per
        # completed generation under the "mlx-backend" service key so
        # per-(tenant, product) markup applies. Registering the App is what gives
        # that emission something true to stamp as `app_id` — without a row the
        # service has no choice but to invent one or stamp NULL, and a nil-tenant
        # /nil-app debit is un-billing, not safe.
        #
        # It does NOT gate access: like karma/hirag/kag-graph above, this is an
        # internal stage reached over the trusted network, and ACCESS entitlement
        # is enforced at the customer-facing surface that calls it. Precedent for
        # an infrastructure app row carrying ZERO licences is already established
        # by `inference-router` and `vllm` below.
        #
        # This key MUST stay byte-identical to the billing service key the backend
        # emits, both "mlx-backend" — license-authz and the per-product markup
        # resolver compare it literally.
        "key": "mlx-backend",
        "issuers": ("platform", "fr"),
        "name": "MLX Backend",
        "audience": "platform",
        "description": "MLX inference backend (Apple-silicon bare metal) — metered per-product usage line; registration only, does not gate access.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the MLX inference backend.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The MLX ingestion backend as a metered SUB-PRODUCT — same shape and same
        # reasoning as `mlx-backend` above. Runs as a local launchd service on
        # port 9120 (see scripts/backfill_tenants_byo_mode.py for the liveness
        # note), self-meters
        # per completed embedding/ingestion call under the "mlx-ingestion-backend"
        # service key, and holds no licences. Registration only; access is enforced
        # at the customer-facing ingestion surface, never at this stage.
        "key": "mlx-ingestion-backend",
        "issuers": ("platform", "fr"),
        "name": "MLX Ingestion Backend",
        "audience": "platform",
        "description": "MLX ingestion/embedding backend (Apple-silicon bare metal) — metered per-product usage line; registration only, does not gate access.",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the MLX ingestion backend.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # Evidence Review (knowledge/evidence_review_service), migration 062. kag-frontend
        # calls it DIRECTLY for a tenant with that tenant's own evidence-review licence
        # (ER_ACCEPTED_LICENCE_APPS), minted through bundle-token as
        # kag-frontend-bundle-minter. It is a DEPENDENCY of kag (models.APP_REQUIRES_SEED),
        # so ensure provisions it on every tenant whose bundles' closure holds kag.
        # `platform`: a service hop, never offered to a person directly.
        "key": "evidence-review",
        "issuers": issuers_for_app_key("evidence-review"),
        "name": "Evidence Review",
        "audience": "platform",
        "description": "Evidence Review service — called by kag-frontend for a tenant with that tenant's own evidence-review licence (dependency of kag, migration 062, 2026-10-15).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for Evidence Review.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The chat-bridge fleet — public LLM chat widgets, bridged to Telegram
        # for live human takeover, each a future self-metering caller of
        # inference-router (sends no `X-App-Id` today — see the description
        # below) (`INFERENCE_ROUTER_URL`, default in-cluster
        # inference-router:8150). No `app`
        # row existed for any of the three as of 2026-09-10 (licenses census:
        # 52 app rows, zero matches), so none could set `APP_ID` at all.
        #
        # ONE row per repo, not one shared row: chat-bridge-rmt's own CLAUDE.md
        # states the fleet forks per brand "instead of multi-tenanting" — the
        # persona/knowledge-base (`src/prompts/site.md`) is hardcoded per fork,
        # not env-driven — and each fork is already its own service by every
        # other measure: separate repo + image
        # (`<your-registry>/<key>`), separate k8s
        # Deployment/Service/namespace, separate Postgres database + role
        # (e.g. chat-bridge-rmt: db + role `chat_bridge_rmt`, "its own role —
        # never reuse another service's DB user"). A single shared app row
        # would cross the same one-service-one-identity boundary this fleet
        # already enforces for its own database.
        #
        # `audience: platform`, not `human`: like mlx-backend /
        # mlx-ingestion-backend above, this is registration so each service
        # has something true to stamp as `app_id` on its own cost_events — it
        # does NOT gate human access. Visitors are anonymous, unauthenticated
        # website chat-widget users, never Keycloak-JWT humans holding a
        # per-tenant licence the way fr/w2v/commonpractice's `human`-audience
        # rows are.
        #
        # No `path`: like mlx-backend/mlx-ingestion-backend, none of these
        # three sit behind api.nonagentic.ai / route-registry.yaml — each is
        # reached at its own public host (nonagentic.ai, merqi.nl,
        # webchat.raisemytickets.com) directly, never through the edge
        # license-authz gate.
        #
        # Owner directive 2026-09-10 (verbatim): "Mint apps, you confirm
        # tenants." This seed mints the app rows ONLY — no licence is minted,
        # no `APP_ID` env var is set anywhere, and no tenant is mapped.
        # Brand-to-tenant mapping is the owner's call, made separately.
        "key": "chat-bridge",
        "issuers": ("platform", "fr"),
        "name": "Chat Bridge",
        "audience": "platform",
        "description": (
            "Public LLM chat widget for nonagentic.ai, bridged to Telegram for "
            "live human takeover — registration only: gives the widget a true "
            "app_id to assert as X-App-Id to inference-router (not sent today); "
            "does not gate access."
        ),
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the chat-bridge widget.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # Sibling fork of `chat-bridge` above — same shape, same reasoning.
        "key": "chat-bridge-merqi",
        "issuers": ("merqi",),
        "name": "Chat Bridge (Merqi)",
        "audience": "platform",
        "description": (
            "Public LLM chat widget for merqi.nl, forked from chat-bridge — "
            "registration only: gives the widget a true app_id to assert as "
            "X-App-Id to inference-router (not sent today); does not gate "
            "access."
        ),
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the chat-bridge-merqi widget.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # Sibling fork of `chat-bridge` above — same shape, same reasoning.
        "key": "chat-bridge-rmt",
        "issuers": ("merqi",),
        "name": "Chat Bridge (RaiseMyTickets)",
        "audience": "platform",
        "description": (
            "Public LLM chat widget for webchat.raisemytickets.com, forked "
            "from chat-bridge — registration only: gives the widget a true "
            "app_id to assert as X-App-Id to inference-router (not sent "
            "today); does not gate access."
        ),
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the chat-bridge-rmt widget.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # core-app — an inference-router caller. Owner decision 2026-10-07:
        # every inference-router caller is a registered app here, and its
        # `X-App-Id` carries this key. Same shape as the chat-bridge fleet /
        # mlx-backend above: `audience: platform`, no `path` (not behind the
        # api.nonagentic.ai edge gate), registration only — gives core-app a
        # true app_id so its router calls attribute to it in billing. Mints no
        # licence and gates no access.
        "key": "core-app",
        "issuers": ("merqi",),
        "name": "Core App",
        "audience": "platform",
        "description": (
            "Core App — registration only: registered so its inference-router "
            "calls attribute to it in billing (X-App-Id); does not gate access."
        ),
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for core-app.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        "key": "raise-my-tickets",
        "issuers": ("merqi",),
        "name": "RaiseMyTickets",
        "audience": "human",
        "description": "RaiseMyTickets white-label ticket-funnel platform — per-tenant license tokens for n8n / external API access (license-authz resolves the token to a verified tenant_id).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for RaiseMyTickets.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # The merqi MARKETING package entitlement — same shape as `w2v` / `fr` /
        # `commonpractice` above, NOT the `chat-bridge-merqi` shape. Those two
        # both carry "merqi" in the name and are otherwise unrelated:
        # chat-bridge-merqi is an anonymous public chat widget, `platform`,
        # registration-only, gating nothing. THIS row is a live ACCESS gate.
        #
        # merqi/marketing enforces it per-tenant in-app: `_APP_KEY = "marketing"`
        # (src/core/license_gate.py), resolved via
        # GET /licenses/active?tenant_id=&app_key=marketing — JWT-mode, so the
        # operator authenticates with a Keycloak JWT (no opaque token) and
        # entitlement is resolved by tenant+app. The gate fails CLOSED: 404 ->
        # 403 `marketing_license_required`, unreachable -> 503
        # `marketing_license_unavailable`. So an absent app row is not a
        # cosmetic gap — no licence can be minted for the key, and the whole app
        # is unusable for every tenant.
        #
        # ⚠️ WHY IT IS HERE AT ALL: the prod row was created ad-hoc through the
        # API, never declared in this baseline. That is exactly the class of gap
        # the header describes — after a DR restore the other 39 keys come back
        # and this one does not, and merqi marketing then 403s tenant-wide with
        # no obvious cause. Declaring it keeps prod a no-op (upsert_app skips
        # keys that already exist) and makes a rebuilt DB correct.
        #
        # No `path` / absent from the route registry: merqi marketing is reached
        # at its own public host, not behind the api.nonagentic.ai edge gate, so
        # license-authz never sees it — enforcement is the in-app gate above.
        "key": "marketing",
        "issuers": ("merqi",),
        "name": "Merqi Marketing",
        "audience": "human",
        "description": "Merqi marketing app — per-tenant access gate enforced in-app at merqi/marketing (JWT-mode, app_key `marketing`).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the merqi marketing app.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # Sibling of `marketing` above — same shape, same reasoning, same
        # fail-closed in-app gate. merqi/sales asserts `_APP_KEY = "sales"`
        # (src/core/license_gate.py) and resolves it via
        # GET /licenses/active?tenant_id=&app_key=sales; 404 -> 403
        # `sales_license_required`, unreachable -> 503.
        "key": "sales",
        "issuers": ("merqi",),
        "name": "Merqi Sales",
        "audience": "human",
        "description": "Merqi sales app — per-tenant access gate enforced in-app at merqi/sales (JWT-mode, app_key `sales`).",
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for the merqi sales app.",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    {
        # Migration 075 (owner decision 2026-10-22): TTS and ASR (mini2 tts-backend /
        # asr-backend, api.nonagentic.ai/tts/ and /asr/) are shared PLATFORM services
        # like vllm / laya, decoupled from Studio (w2v). ONE key for both: a licence
        # token is per app and agent-api sends one VOICE_TOKEN to both backends.
        # vllm / laya's shape (block below) with an explicit issuer: platform only
        # (models.APP_ISSUER_OVERRIDES). Pinned equal to the migration by
        # tests/unit/test_migration_075_speech_platform_app.py.
        "key": "speech",
        "issuers": issuers_for_app_key("speech"),
        "name": "Speech (TTS / ASR)",
        "audience": "platform",
        "description": (
            "Speech (TTS / ASR) — access gate for `/tts/` and `/asr/` on "
            "api.nonagentic.ai, enforced at the edge by license-authz (app_key "
            "`speech`). Shared platform service, migration 075."
        ),
        "plan": {
            "code": "standard",
            "name": "Standard",
            "description": "Default per-tenant plan for Speech (TTS / ASR).",
            "rate_limit_rpm": 600,
            "rate_limit_burst": 60,
            "duration_days": None,  # perpetual
            "quota_monthly": DEFAULT_QUOTA_MONTHLY,
            "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
        },
    },
    *[
        {
            # Migration 078 (care-bus early access, 2026-10-22): the strata tools care-bus
            # calls for a tenant — shared platform services like `speech` (above) and
            # `mcp` (below), issued by platform only (models.APP_ISSUER_OVERRIDES). Its
            # tenants hold them as DEPENDENCY licences of care-bus
            # (models.APP_REQUIRES_SEED). Pinned equal to the migration by
            # tests/unit/test_migration_078_care_bus_early_access.py.
            "key": key,
            "issuers": issuers_for_app_key(key),
            "name": name,
            "audience": "platform",
            "description": (
                f"{name} — {what} for a tenant, called by care-bus with a licence of "
                "that tenant (bundle-token; a dependency of care-bus). Shared platform "
                "service, migration 078."
            ),
            "plan": {
                "code": "standard",
                "name": "Standard",
                "description": f"Default per-tenant plan for {name}.",
                "rate_limit_rpm": 600,
                "rate_limit_burst": 60,
                "duration_days": None,  # perpetual
                "quota_monthly": DEFAULT_QUOTA_MONTHLY,
                "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
            },
        }
        for key, name, what in [
            ("strata-rag", "Strata RAG", "retrieval"),
            ("strata-ingest", "Strata Ingest", "document ingestion"),
        ]
    ],
    # -----------------------------------------------------------------------
    # route-registry-derived ACCESS gates — the COMPLETE baseline.
    #
    # Every entry below is a public route through api.nonagentic.ai whose
    # `app_key` license-authz already enforces at the edge — see
    # gitops-production/clusters/strato/route-registry.yaml, the single source
    # of truth for app_key. Without an `app` row the key is unlicensable: no
    # license can be minted for it, so the route is reachable by nobody.
    #
    # This block mirrors the registry 1:1 (28 keys as of 2026-07-17), which is
    # the point — it is a BASELINE, not a changelog of past gaps. Of these, 13
    # were enforced-but-unseeded on prod and 11 more existed on prod only
    # because someone created them ad-hoc through the API, so a fresh DB came
    # up missing 24 of 28. Declaring the full set makes a rebuilt DB correct
    # and keeps prod a no-op (upsert_app skips keys that already exist).
    #
    # Keys MUST stay byte-identical to the registry's `app_key`: license-authz
    # compares them literally, so a near-miss (see migration 015's
    # skill_builder/skill-builder) fails the edge check against a license that
    # looks valid in the DB and in the admin console.
    #
    # Limits are deliberately uniform (600 rpm / 60 burst, perpetual) — a
    # starting posture, not a considered per-route SLO. Tune per app via the
    # licenses admin console (fr /settings/admin/licenses) rather than here.
    # -----------------------------------------------------------------------
    *[
        {
            "key": key,
            # Migration 050 rule: inference-router / kag carry explicit issuers,
            # the rest of the gate set is the platform long tail {platform, fr}.
            "issuers": issuers_for_app_key(key),
            "name": name,
            # Uniformly platform: this whole block IS the service-to-service set —
            # edge access gates on api.nonagentic.ai that fr drives on the user's
            # behalf. That is the rule the comment above has always stated in prose;
            # `audience` is where it stops being prose.
            "audience": "platform",
            "description": (
                f"{name} — access gate for `{path}` on api.nonagentic.ai, "
                f"enforced at the edge by license-authz (app_key `{key}`)."
            ),
            "plan": {
                "code": "standard",
                "name": "Standard",
                "description": f"Default per-tenant plan for {name}.",
                "rate_limit_rpm": 600,
                "rate_limit_burst": 60,
                "duration_days": None,  # perpetual
                "quota_monthly": DEFAULT_QUOTA_MONTHLY,
                "token_budget_monthly": DEFAULT_TOKEN_BUDGET_MONTHLY,
            },
        }
        for key, name, path in [
            # core
            ("workers", "Worker Management", "/workers/"),
            ("kanban", "Kanban", "/kanban/"),
            ("kag", "KAG", "/kag/"),
            ("secrets", "Secrets", "/secrets/"),
            ("tools", "Tools", "/tools/"),
            ("artifacts", "Artifacts", "/artifacts/"),
            ("procedures", "Procedures", "/procedures/"),
            ("requirements", "Requirements", "/requirements/"),
            ("mcp", "MCP", "/mcp/"),
            ("coding", "Coding", "/coding/"),
            ("licenses", "Licenses API", "/licenses/"),
            ("skill-builder", "Skill Builder", "/skills/"),
            # flywheel
            ("inference-router", "Inference Router", "/inference-router/"),
            ("model-registry", "Model Registry", "/model-registry/"),
            ("data-collector", "Data Collector", "/data-collector/"),
            ("model-evaluator", "Model Evaluator", "/model-evaluator/"),
            (
                "training-data-generator",
                "Training Data Generator",
                "/training-data-generator/",
            ),
            ("improvement-loop", "Improvement Loop", "/improvement-loop/"),
            ("vllm", "vLLM Inference", "/vllm/"),
            # pgx laya (Jev-compatible System-1 classifier) behind its Jev
            # bridge (gitops-production scripts/host-configs/pgx/laya/).
            ("laya", "Laya System-1 (Jev API)", "/laya/"),
            # kag
            ("kag-orchestrator", "KAG Orchestrator", "/kag-orchestrator/"),
            ("graph-adapter", "Graph Adapter", "/graph-adapter/"),
            ("nlp-service", "NLP Service", "/nlp/"),
            # storage
            ("storage", "Storage API", "/storage/"),
            # infra
            ("npm", "NPM Registry", "/npm/"),
        ]
    ],
]


def all_plans(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The app's `standard` plan followed by any `extra_plans` (e.g. agent/byo, 071)."""
    return [spec["plan"], *spec.get("extra_plans", ())]


def upsert_app(db: DatabaseManager, spec: dict[str, Any]) -> App:
    existing = db.get_app_by_key(spec["key"])
    if existing is not None:
        # `audience` is reconciled on an existing app; every other field is left
        # alone. The rest are descriptive and an operator may legitimately have
        # edited them, but audience is a CLASSIFICATION this file is the source of
        # truth for — and it gates whether the app is offered to people at all.
        # Without this branch a wrong value could never be corrected by re-seeding,
        # only by hand-written SQL.
        want = spec.get("audience", "platform")
        if existing.audience != want:
            db.set_app_audience(spec["key"], want)
            logger.info(
                "app %s audience %s -> %s (reconciled)", spec["key"], existing.audience, want
            )
        else:
            logger.info("app %s already present (id=%s) — skip", spec["key"], existing.id)
        return existing
    created = db.create_app(
        key=spec["key"],
        name=spec["name"],
        description=spec.get("description"),
        audience=spec.get("audience", "platform"),
        issuers=spec["issuers"],
    )
    logger.info("app %s created (id=%s)", created.key, created.id)
    return created


def upsert_plan(db: DatabaseManager, app: App, plan_spec: dict[str, Any]) -> Plan:
    # Plan uniqueness = (app_id, code, region_id). region_id=NULL for global.
    # `app.id` is typed by SQLAlchemy as `Column[UUID]`; cast for the call
    # site that wants a raw `UUID`.
    app_uuid = cast(UUID, app.id)
    session = db.get_session()
    try:
        existing = (
            session.query(Plan)
            .filter(
                Plan.app_id == app_uuid,
                Plan.code == plan_spec["code"],
                Plan.region_id.is_(None),
            )
            .first()
        )
        if existing is not None:
            logger.info(
                "plan %s/%s already present (id=%s) — skip",
                app.key,
                plan_spec["code"],
                existing.id,
            )
            return existing
    finally:
        session.close()
    created = db.create_plan(
        app_id=app_uuid,
        code=plan_spec["code"],
        name=plan_spec["name"],
        description=plan_spec.get("description"),
        region_id=None,
        duration_days=plan_spec.get("duration_days"),
        rate_limit_rpm=plan_spec.get("rate_limit_rpm"),
        rate_limit_burst=plan_spec.get("rate_limit_burst"),
        quota_monthly=plan_spec.get("quota_monthly"),
        token_budget_monthly=plan_spec.get("token_budget_monthly"),
    )
    logger.info(
        "plan %s/%s created (id=%s, rpm=%s, burst=%s)",
        app.key,
        created.code,
        created.id,
        created.rate_limit_rpm,
        created.rate_limit_burst,
    )
    return created


class MissingIssuerError(ValueError):
    """An app spec declares no issuing owner (migration 050)."""


def check_issuers(apps: list[dict[str, Any]]) -> None:
    """Every app MUST declare at least one issuing owner, or seeding fails.

    Migration 050: an app with no ``app_issuer`` row cannot have a licence minted
    or revoked by any client except the break-glass one, so declaring an app
    without saying who issues it is a defect, not a default.
    """
    missing = [spec["key"] for spec in apps if not spec.get("issuers")]
    if missing:
        raise MissingIssuerError(
            f"app(s) declared without issuers: {', '.join(missing)} — add an "
            "'issuers' tuple of tenant_owner keys to each spec"
        )


def upsert_issuers(db: DatabaseManager, app: App, spec: dict[str, Any]) -> None:
    inserted = db.add_app_issuers(cast(UUID, app.id), spec["issuers"])
    if inserted:
        logger.info("app %s issuers += %s", app.key, ", ".join(spec["issuers"]))


def main() -> int:
    try:
        check_issuers(APPS)
    except MissingIssuerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    db.create_tables()  # safe no-op when tables already exist
    db.seed_ownership_reference()  # tenant_owner rows the issuers reference

    for spec in APPS:
        app = upsert_app(db, spec)
        for plan_spec in all_plans(spec):
            upsert_plan(db, app, plan_spec)
        upsert_issuers(db, app, spec)

    logger.info("seed_apps: complete (%d apps)", len(APPS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
