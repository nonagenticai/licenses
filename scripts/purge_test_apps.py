#!/usr/bin/env python3
"""Purge integration-test / demo residue from the app catalog.

WHY THIS EXISTS
    Integration tests have been creating apps directly in the licenses DB and
    never cleaning up. On prod (audited 2026-07-17) that left 278 app rows of
    which 242 were junk — `inttest-<hex>` x163, `demo-app-<hex>` x70, plus
    dbg/debug/test/probe one-offs — carrying 194 plans, 144 licenses and 227
    tokens. Two of those rows are keyed `inttest-{uuid}` and
    `inttest-<hex>.hex[:8]}`: literal unformatted f-strings, i.e. test code
    writing its own source text into prod.

    That makes every "pick an app" surface unusable (a 278-entry picker) and
    makes the catalog an unreliable answer to "what can I license?".

    This script cleans the SYMPTOM. The CAUSE — a test config pointed at the
    prod DB — is not fixed here; until it is, this is re-runnable hygiene
    rather than a one-off.

SAFETY MODEL (allowlist-first, not regex-first)
    A pattern match alone must never authorize a delete: the blast radius is a
    hard cascade (app -> plans -> licenses -> tokens). So every app row is
    classified into exactly one of three buckets:

      KEEP    — key is in the seed baseline, or in KEEP_EXTRA (real apps that
                predate the seed and were created ad-hoc through the API)
      JUNK    — key matches a known test/demo shape AND is not in KEEP
      UNKNOWN — neither: the script ABORTS and prints them

    UNKNOWN is the important bucket. A new real app nobody added to the seed,
    or a new test-key shape, both land there and stop the run for a human —
    rather than being silently deleted or silently kept.

    ⛔ The n8n-dev tenant holds long-lived license tokens that must never be
    touched (see the Movies CLAUDE.md hard rule). Those live on REAL apps
    (artifacts, coding, kag, kanban, licenses, mcp, kaizen_*, meta_prompt_*,
    keycloak) — all KEEP. n8n-dev also holds 88 junk licenses / 102 tokens on
    `inttest-*`/`demo-app-*` apps, which are test residue and DO go. The
    guard that keeps those two sets apart is the KEEP allowlist, and
    `--apply` re-asserts it per row immediately before each delete.

USAGE
    # inspect (default — never deletes):
    DATABASE_URL="postgresql+psycopg://…/licenses" python scripts/purge_test_apps.py

    # execute, after taking a dump:
    DATABASE_URL=… python scripts/purge_test_apps.py --apply

Exit codes: 0 = clean/complete; 1 = a delete failed; 2 = bad env; 3 = UNKNOWN
apps present (nothing deleted).
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import App, DatabaseManager, License, Plan, Token  # noqa: E402

logger = logging.getLogger("purge_test_apps")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

_SEED = Path(__file__).resolve().parent / "seed_apps.py"

# Real apps that exist on prod but predate scripts/seed_apps.py — they were
# created ad-hoc through the API, so the seed does not declare them and they
# would otherwise classify as UNKNOWN. Several carry the protected n8n-dev
# tokens. Do NOT prune this list to "tidy up": an omission here is a delete.
KEEP_EXTRA: frozenset[str] = frozenset(
    {
        "book-fulfillment",
        "kaizen_core_actions",
        "kaizen_discover_actions",
        "keycloak",
        "marketing",
        "meta_prompt_core_actions",
        "meta_prompt_discover_actions",
        "metering",
        "n8n",
        "n8n-gerard",
        "observability",
        "procedure_management",
        "sales",
        "w2v-image",
        # Pre-migration-015 spelling of `skill-builder`. Migration 015 renames
        # it in place; if the purge runs on a DB where 015 has not yet applied,
        # this keeps its 5 licenses alive rather than classifying it UNKNOWN.
        "skill_builder",
    }
)

# Known test/demo key shapes, anchored. Matching is necessary but NOT
# sufficient — KEEP always wins (see classify()).
JUNK_RE = re.compile(
    r"""^(?:
          inttest-        # integration-test apps (incl. the literal
                          # `inttest-{uuid}` f-string bugs)
        | demo-app(?:-|\Z) # demo-app, demo-app-<epoch-ms>
        | test-           # test-<hex>, test-app-delete-me
        | dbg\d*-         # dbg2-<hex>, dbg3-<hex>
        | debug-          # debug-<hex>
        | probe-          # probe-app-del-me
        | app-            # generic ad-hoc scratch apps
    )""",
    re.VERBOSE,
)


def _seeded_keys() -> set[str]:
    """Load `APPS` from the seed module by path (same trick the unit test uses)."""
    spec = importlib.util.spec_from_file_location("seed_apps", _SEED)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load seed module at {_SEED}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {a["key"] for a in cast(list[dict[str, Any]], mod.APPS)}


def classify(keys: list[str], keep: set[str]) -> tuple[list[str], list[str], list[str]]:
    kept = [k for k in keys if k in keep]
    junk = [k for k in keys if k not in keep and JUNK_RE.match(k)]
    unknown = [k for k in keys if k not in keep and not JUNK_RE.match(k)]
    return sorted(kept), sorted(junk), sorted(unknown)


def blast_radius(db: DatabaseManager, junk_keys: list[str]) -> dict[str, Any]:
    """Count exactly what a purge would destroy, before destroying it."""
    session = db.get_session()
    try:
        app_ids = [r[0] for r in session.query(App.id).filter(App.key.in_(junk_keys)).all()]
        if not app_ids:
            return {"apps": 0, "plans": 0, "licenses": 0, "tokens": 0, "tenants": []}
        lic_ids = [
            r[0] for r in session.query(License.id).filter(License.app_id.in_(app_ids)).all()
        ]
        tenants = sorted(
            {
                # str(): tenant_id is a UUID column since migration 016; downstream
                # this set is "\n".join()'d (str-only) — a raw UUID would TypeError.
                str(r[0])
                for r in session.query(License.tenant_id)
                .filter(License.app_id.in_(app_ids))
                .distinct()
                .all()
                if r[0]
            }
        )
        return {
            "apps": len(app_ids),
            "plans": session.query(Plan).filter(Plan.app_id.in_(app_ids)).count(),
            "licenses": len(lic_ids),
            "tokens": (
                session.query(Token).filter(Token.license_id.in_(lic_ids)).count() if lic_ids else 0
            ),
            "tenants": tenants,
        }
    finally:
        session.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually delete. Omit for a dry run (the default).",
    )
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    keep = _seeded_keys() | KEEP_EXTRA

    session = db.get_session()
    try:
        all_keys = [r[0] for r in session.query(App.key).all()]
    finally:
        session.close()

    kept, junk, unknown = classify(all_keys, keep)
    logger.info(
        "catalog: %d apps — keep=%d junk=%d unknown=%d",
        len(all_keys),
        len(kept),
        len(junk),
        len(unknown),
    )

    if unknown:
        logger.error(
            "ABORT: %d app(s) match neither the keep-list nor a known test shape:\n  %s",
            len(unknown),
            "\n  ".join(unknown),
        )
        logger.error(
            "Each is either (a) a REAL app missing from scripts/seed_apps.py / "
            "KEEP_EXTRA — add it, or (b) a NEW test-key shape — extend JUNK_RE. "
            "Refusing to guess; nothing was deleted."
        )
        return 3

    if not junk:
        logger.info("nothing to purge — catalog is clean")
        return 0

    radius = blast_radius(db, junk)
    logger.info(
        "blast radius: %d apps -> %d plans, %d licenses, %d tokens",
        radius["apps"],
        radius["plans"],
        radius["licenses"],
        radius["tokens"],
    )
    logger.info(
        "tenants holding a license on a junk app: %d\n  %s",
        len(radius["tenants"]),
        "\n  ".join(radius["tenants"]) or "(none)",
    )
    logger.info("junk keys:\n  %s", "\n  ".join(junk))

    if not args.apply:
        logger.info("DRY RUN — nothing deleted. Re-run with --apply (dump first).")
        return 0

    deleted = 0
    for key in junk:
        # Re-assert the allowlist per row: `junk` was computed from a snapshot,
        # and this is the last gate before an irreversible cascade.
        if key in keep or not JUNK_RE.match(key):
            logger.error("REFUSING to delete %r — failed the re-check", key)
            return 1
        session = db.get_session()
        try:
            app = session.query(App).filter(App.key == key).first()
            app_id = cast(UUID, app.id) if app else None
        finally:
            session.close()
        if app_id is None:
            logger.info("%s already gone — skip", key)
            continue
        # Reuse the same cascade the API's DELETE /apps/{id} uses: it removes
        # licenses BEFORE the app, because license.plan_id is ON DELETE
        # RESTRICT and would otherwise block the plan cascade.
        if db.delete_app(app_id):
            deleted += 1
            logger.info("deleted %s (%s)", key, app_id)
        else:
            logger.error("delete failed for %s (%s)", key, app_id)
            return 1

    logger.info("purge complete: %d app(s) deleted", deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
