#!/usr/bin/env python3
r"""
Idempotent baseline seed for commercial Offerings. Runs against the licenses
Postgres on each release; safe to re-run (``create_offering`` upserts on
``(app, code)``).

An Offering is the per-product commercial policy (byo posture + usage markup +
optional access fee/allowance) that ``POST /tenants/{t}/offerings/{app}/{code}``
materialises onto a tenant (a billing markup rule + optional access charge).
Offerings are NOT created by any migration — a fresh licenses DB has none, so
the provisioning endpoint 404s until they exist. This seed closes that gap.

Bundles: the **KAG pipeline** — the four internal metered stages
(``ingestion`` / ``karma`` / ``hirag`` / ``kag-graph``) grouped under one shared
offering ``code`` (``kag-pipeline``). It is a PURE-USAGE offering (George,
2026-07-09): no access fee, no included allowance — the customer pays only for
what they run, at cost×(1+markup). ACCESS to KAG is gated at the customer-facing
surface (the ``fr`` package), never at an internal stage, so these stage
offerings carry no access charge.

And the **strata app** ``care-bus`` (migration 067; ``strata-configurator`` was
retired by migration 084): one ``standard`` offering, ``byo_required`` — BYO only,
no platform-paid offering, no access fee. The **agent** app (migration 070) takes
the same posture.

``byo_mode`` is ``byo_required`` (not ``platform_only``) deliberately: it is the
revenue-safe *commercial* posture. ``platform_only``/``local_allowed`` live in
billing's internal/$0 set (``services.byo_mode._INTERNAL_BYO_MODES``); tagging a
paying offering with either risks a $0 bill if the billing gate ever becomes
per-product-aware. The "platform runs the GPU" fact is carried by the metered
usage's ``self_hosted``/``owner_provided`` cost basis, not the access posture.

Usage prices for the four stages are seeded separately in billing
(``billing_service/scripts/initial_pricing.yaml`` — provider_pricing per_request,
market÷1.3). This offering only pins the markup layered on top.

Usage:
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \  # pragma: allowlist secret
        python scripts/seed_offerings.py

In-cluster: run in the licenses pod, or ship as a k8s Job manifest in
gitops-production (mirrors seed_apps.py).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# Make `models` importable when run from the repo root or inside the licenses
# container (working dir = /app).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402

logger = logging.getLogger("seed_offerings")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

# The customer-facing KAG-pipeline commercial bundle. One Offering row per stage
# App (an Offering is strictly one App), sharing the ``kag-pipeline`` code so the
# four are one logical bundle. Add new commercial bundles here as they are agreed.
KAG_PIPELINE_MARKUP_PCT = 30  # platform two-axis default → customer pays cost×1.30

OFFERINGS: list[dict[str, Any]] = [
    {
        "app_key": "ingestion",
        "code": "kag-pipeline",
        "name": "KAG Pipeline — Ingestion / Webscraper",
        "byo_mode": "byo_required",
        "usage": {"markup_pct": KAG_PIPELINE_MARKUP_PCT},
        "notes": "KAG pipeline bundle (pure usage, no access fee). Webscraper page ingest.",
    },
    {
        "app_key": "karma",
        "code": "kag-pipeline",
        "name": "KAG Pipeline — KARMA Enrichment",
        "byo_mode": "byo_required",
        "usage": {"markup_pct": KAG_PIPELINE_MARKUP_PCT},
        "notes": "KAG pipeline bundle (pure usage, no access fee). Entity extraction / enrichment.",
    },
    {
        "app_key": "hirag",
        "code": "kag-pipeline",
        "name": "KAG Pipeline — HiRAG Decomposition",
        "byo_mode": "byo_required",
        "usage": {"markup_pct": KAG_PIPELINE_MARKUP_PCT},
        "notes": "KAG pipeline bundle (pure usage, no access fee). Hierarchical decomposition.",
    },
    {
        "app_key": "kag-graph",
        "code": "kag-pipeline",
        "name": "KAG Pipeline — KAG Graph Build",
        "byo_mode": "byo_required",
        "usage": {"markup_pct": KAG_PIPELINE_MARKUP_PCT},
        "notes": "KAG pipeline bundle (pure usage, no access fee). Graph/subgraph build.",
    },
    # The strata apps (migration 067): BYO-only — the tenant brings its own model
    # keys; there is NO platform-paid offering (no `platform_only` /
    # `local_allowed` row, no access fee, no usage markup layered by this seed).
    # strata-configurator was retired by migration 084 (merged into `agent`), so
    # only care-bus remains.
    {
        "app_key": "care-bus",
        "code": "standard",
        "name": "Care Bus — Standard (BYO)",
        "byo_mode": "byo_required",
        "notes": "BYO only; no platform-paid offering exists for this app.",
    },
    # The agent app (migration 070): BYO-only, the strata apps' posture.
    {
        "app_key": "agent",
        "code": "standard",
        "name": "Agent — Standard (BYO)",
        "byo_mode": "byo_required",
        "notes": "BYO only; no platform-paid offering exists for this app.",
    },
]


def upsert_offering(db: DatabaseManager, spec: dict[str, Any]) -> bool:
    """Upsert one offering; returns True on success, False if the app is missing."""
    result = db.create_offering(
        spec["app_key"],
        spec["code"],
        spec["name"],
        spec["byo_mode"],
        usage=spec.get("usage"),
        access_price_usd=spec.get("access_price_usd"),
        access_cadence=spec.get("access_cadence"),
        included_allowance_usd=spec.get("included_allowance_usd"),
        notes=spec.get("notes"),
    )
    if result is None:
        logger.error(
            "offering %s/%s SKIPPED — app %r not found (run seed_apps.py first)",
            spec["app_key"],
            spec["code"],
            spec["app_key"],
        )
        return False
    logger.info(
        "offering %s/%s upserted (byo_mode=%s markup_pct=%s access_price=%s allowance=%s)",
        result["app_key"],
        result["code"],
        result["byo_mode"],
        result.get("markup_pct"),
        result.get("access_price_usd"),
        result.get("included_allowance_usd"),
    )
    return True


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    db.create_tables()  # safe no-op when tables already exist

    missing = 0
    for spec in OFFERINGS:
        if not upsert_offering(db, spec):
            missing += 1

    logger.info("seed_offerings: complete (%d offerings, %d missing-app)", len(OFFERINGS), missing)
    # Missing app = a real gap (apps must be seeded first) → non-zero exit.
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
