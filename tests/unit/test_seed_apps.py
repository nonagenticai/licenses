"""Unit guards for the baseline app seed (``scripts/seed_apps.py``).

Pure-unit: loads the seed module's ``APPS`` list by file path and asserts its
shape. No DB — the idempotent upsert path is exercised by the integration
provisioning suite. These guards catch a duplicate key or a malformed new entry
(e.g. the P5 ``ingestion`` product) before it ships to the release Job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_apps.py"


def _load_seed_module() -> Any:
    """The seed MODULE (not just APPS) — the shared-constant guard needs both."""
    spec = importlib.util.spec_from_file_location("seed_apps", _SEED)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_apps() -> list[dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("seed_apps", _SEED)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.APPS


def test_app_keys_are_unique() -> None:
    keys = [a["key"] for a in _load_apps()]
    assert len(keys) == len(set(keys)), f"duplicate app key in seed: {keys}"


def test_every_app_has_a_standard_plan() -> None:
    # Precondition, not decoration: every assertion below is inside the loop,
    # so an empty (or import-broken) APPS list would report GREEN having
    # checked nothing.
    apps = _load_apps()
    assert apps, "seed_apps.APPS is empty — this guard would check nothing"
    for app in apps:
        assert app.get("name"), f"app {app.get('key')!r} missing name"
        plan = app.get("plan")
        assert isinstance(plan, dict) and plan.get("code") == "standard", (
            f"app {app['key']!r} must carry a 'standard' plan"
        )


def test_ingestion_product_is_seeded() -> None:
    """P5: the webscraper/ingestion product must be a licensable App.

    Its key MUST equal ``ingestion`` — the same value used as the billing
    ``service_key`` and the S2S posture/entitlement ``app_key`` — so markup,
    entitlement, and posture all resolve to one product.
    """
    ing = next((a for a in _load_apps() if a["key"] == "ingestion"), None)
    assert ing is not None, "ingestion product missing from the app seed"
    assert ing["plan"]["code"] == "standard"


def test_karma_product_is_seeded() -> None:
    """P6: the KARMA enrichment stage must be a metered sub-product App.

    Its key MUST equal ``karma`` — the same value karma_service emits as the
    billing ``service_key`` and the consumer path prefix — so per-(tenant,
    product) markup resolves to one product. Registration only; karma is an
    internal KAG stage and does not gate access.
    """
    km = next((a for a in _load_apps() if a["key"] == "karma"), None)
    assert km is not None, "karma product missing from the app seed"
    assert km["plan"]["code"] == "standard"


def test_hirag_product_is_seeded() -> None:
    """P7: the HiRAG decomposition stage must be a metered sub-product App.

    Its key MUST equal ``hirag`` — the same value hirag_service emits as the
    billing ``service_key`` and the consumer path prefix — so per-(tenant,
    product) markup resolves to one product. Registration only; hirag is an
    internal KAG stage and does not gate access.
    """
    hg = next((a for a in _load_apps() if a["key"] == "hirag"), None)
    assert hg is not None, "hirag product missing from the app seed"
    assert hg["plan"]["code"] == "standard"


def test_kag_graph_product_is_seeded() -> None:
    """P7: the KAG-graph build stage must be a metered sub-product App.

    Its key MUST equal ``kag-graph`` — the same value kag_graph_service emits as
    the billing ``service_key`` and the consumer path prefix — so per-(tenant,
    product) markup resolves to one product. Registration only; kag-graph is an
    internal KAG stage and does not gate access.
    """
    kg = next((a for a in _load_apps() if a["key"] == "kag-graph"), None)
    assert kg is not None, "kag-graph product missing from the app seed"
    assert kg["plan"]["code"] == "standard"


def test_mlx_backends_are_seeded() -> None:
    """mlx-backend / mlx-ingestion-backend must exist so they can self-meter.

    Both run on Apple-silicon bare metal (mini2, launchd) with no edge route, so
    they are absent from the route registry and no ACCESS gate would ever create
    them. They still emit cost_events, and a cost_event needs a true ``app_id``:
    without these rows the services must stamp NULL or invent one, and an
    unattributed debit is un-billing. Infrastructure-as-an-app-row-with-zero-
    licences is the established shape here (``inference-router``, ``vllm``).

    Keys are byte-identical to the billing service keys the backends emit.
    """
    seeded = {a["key"]: a for a in _load_apps()}
    for key in ("mlx-backend", "mlx-ingestion-backend"):
        assert key in seeded, f"{key} missing from the app seed"
        assert seeded[key]["audience"] == "platform"
        assert seeded[key]["plan"]["code"] == "standard"


def test_chat_bridge_fleet_is_seeded() -> None:
    """chat-bridge / chat-bridge-merqi / chat-bridge-rmt must each be seeded.

    Each is its own fork (own repo, image, k8s Deployment/Service/namespace,
    Postgres database + role) per chat-bridge-rmt's own CLAUDE.md, not one
    service multi-tenanted across brands — so this is THREE rows, not one.
    `audience` is `platform`: registration gives each a true app_id to assert
    as `X-App-Id` to inference-router in future (none of the three send it
    today — see the seed's own description strings); it does not gate human
    access (visitors are anonymous). None of the three are in the
    route-registry set below — they are reached at their own public host, not
    behind the api.nonagentic.ai edge gate.
    """
    seeded = {a["key"]: a for a in _load_apps()}
    for key in ("chat-bridge", "chat-bridge-merqi", "chat-bridge-rmt"):
        assert key in seeded, f"{key} missing from the app seed"
        assert seeded[key]["audience"] == "platform"
        assert seeded[key]["plan"]["code"] == "standard"
        assert key not in ROUTE_REGISTRY_APP_KEYS


def test_core_app_is_seeded() -> None:
    """core-app must be seeded so its inference-router calls attribute in billing.

    Owner decision: every inference-router caller is a registered app and its
    ``X-App-Id`` carries the app key. Without a ``core-app`` row the router's
    cost_events for core-app have no true ``app_id``. Registration only:
    ``platform`` audience, not behind the api.nonagentic.ai edge gate.
    """
    seeded = {a["key"]: a for a in _load_apps()}
    assert "core-app" in seeded, "core-app missing from the app seed"
    assert seeded["core-app"]["audience"] == "platform"
    assert seeded["core-app"]["plan"]["code"] == "standard"
    assert "core-app" not in ROUTE_REGISTRY_APP_KEYS


def test_merqi_marketing_and_sales_are_seeded() -> None:
    """merqi `marketing` / `sales` must be in the BASELINE, not just on prod.

    Both are live, load-bearing ACCESS gates enforced in-app, per-tenant:
    marketing asserts ``_APP_KEY = "marketing"`` and sales ``_APP_KEY = "sales"``
    (each repo's ``src/core/license_gate.py``), resolved via
    ``GET /licenses/active?tenant_id=&app_key=<key>``. Both fail CLOSED — 404
    becomes 403 ``<app>_license_required``, an unreachable licenses service
    becomes 503 — so with no ``app`` row no licence can be minted for the key
    and the app is unusable for every tenant.

    Their prod rows were created ad-hoc through the API and were never declared
    here. This seed is what a DR restore re-runs, so before this guard a
    rebuilt DB came up with the other keys and without these two, and both
    merqi apps 403'd tenant-wide with no obvious cause. Same class as the 24
    route-registry keys below.

    ``audience`` is ``human``, not ``platform``: per ``models.App``, ``human``
    is "an end-user product a person is licensed to use" and ``platform`` is
    service-to-service that fr drives on the user's behalf. These are
    operator-facing merqi apps whose gate runs against a Keycloak-JWT human's
    tenant — the ``fr``/``w2v``/``commonpractice`` shape. ``audience`` also
    decides whether fr's provisioning picker offers the app at all, so
    ``platform`` here would make the licence unmintable through the UI.

    Neither is in the route registry: each is reached at its own public host,
    not behind the api.nonagentic.ai edge gate, so license-authz never sees it.
    """
    seeded = {a["key"]: a for a in _load_apps()}
    for key in ("marketing", "sales"):
        assert key in seeded, f"{key} missing from the app seed"
        assert seeded[key]["audience"] == "human"
        assert seeded[key]["plan"]["code"] == "standard"
        assert key not in ROUTE_REGISTRY_APP_KEYS


# Every `app_key` in the edge's route registry, pinned as of 2026-07-17.
# SOURCE OF TRUTH: gitops-production/clusters/strato/route-registry.yaml.
# Pinned rather than parsed because that file lives in another repo and is not
# available to this suite; the registry's own "To onboard a new app" checklist
# carries the step that keeps the two in step.
ROUTE_REGISTRY_APP_KEYS = frozenset(
    {
        # core
        "workers",
        "kanban",
        "kag",
        "secrets",
        "tools",
        "artifacts",
        "procedures",
        "requirements",
        "mcp",
        "coding",
        "licenses",
        "skill-builder",
        # flywheel
        "inference-router",
        "model-registry",
        "data-collector",
        "model-evaluator",
        "training-data-generator",
        "improvement-loop",
        "vllm",
        "laya",
        # kag
        "kag-orchestrator",
        "graph-adapter",
        "nlp-service",
        "kag-graph",
        "karma",
        "hirag",
        # storage / infra
        "storage",
        "npm",
        "ingestion",
    }
)


def test_every_route_registry_app_key_is_seeded() -> None:
    """An enforced app_key with no App row is unlicensable — a silent lockout.

    license-authz gates each public route on the `app_key` from the route
    registry. Minting a license requires an `app` row, so a registry key absent
    from the catalog cannot be licensed by anyone, through any UI. 13 keys were
    in exactly that state on prod until 2026-07-17 (and 11 more existed only
    because they had been created ad-hoc through the API, so a rebuilt DB came
    up missing 24 of 28). This is the regression guard for that whole class.
    """
    seeded = {a["key"] for a in _load_apps()}
    missing = sorted(ROUTE_REGISTRY_APP_KEYS - seeded)
    assert not missing, (
        f"route-registry app_key(s) absent from the seed and therefore unlicensable: {missing}"
    )


def test_legacy_skill_builder_key_is_not_seeded() -> None:
    """`skill_builder` (underscore) is the pre-migration-015 spelling.

    The registry enforces `skill-builder`; license-authz compares app_key
    literally, so re-introducing the underscore spelling would mint licenses
    that look valid everywhere but fail closed at the edge.
    """
    seeded = {a["key"] for a in _load_apps()}
    assert "skill_builder" not in seeded, (
        "seed re-introduces the legacy `skill_builder` key — migration 015 "
        "renames it to `skill-builder`; seeding it back recreates the split"
    )
    assert "skill-builder" in seeded


def test_no_seeded_plan_is_unlimited() -> None:
    """Every seeded plan MUST carry both allowances — this is where the 42
    uncapped prod plan rows came from.

    ⚠️ `None` is NOT "unset". Per migration 017 the stored contract is
    ``NULL = unlimited``, ``0 = block-all``, ``>0 = monthly cap``. A plan seeded
    with NULLs is therefore an explicitly UNCAPPED plan, and the metering chain
    honours it end to end: `run_metering_reconcile` selects only licences on a
    plan that owes billing a quota row or a token budget, so billing holds no
    `token_budgets` row and skips every debit with
    ``{"event":"budget_debit_skipped","reason":"no_active_budgets"}`` at INFO —
    no ceiling, no alert, nothing to see in a dashboard.

    Measured 2026-08-11 before migration 020: 42 of 88 prod plan rows were
    unlimited, covering 43 of 141 live licences, and 5 of the last 7 licences
    minted landed on one. George: "every plan needs to be metered! that's the
    whole point of having metering!"

    0 is deliberately ALLOWED here — that is block-all, a real (and stricter)
    meter, not an absent one.
    """
    apps = _load_apps()
    assert apps, "seed_apps.APPS is empty — this guard would check nothing"
    unlimited = [
        f"{app['key']}/{plan['code']}"
        for app in apps
        for plan in [app["plan"], *app.get("extra_plans", ())]
        if plan.get("quota_monthly") is None or plan.get("token_budget_monthly") is None
    ]
    assert not unlimited, (
        "these seeded apps would create an UNLIMITED (unmetered) plan: "
        f"{unlimited}. Set quota_monthly and token_budget_monthly — "
        "seed_apps.DEFAULT_QUOTA_MONTHLY / DEFAULT_TOKEN_BUDGET_MONTHLY."
    )


def test_seeded_allowances_are_positive_and_shared() -> None:
    """The GREEN half: every plan carries the SAME two constants.

    Guards the other failure direction — a future edit that meters one app and
    forgets the rest would satisfy the test above only if it covered all of them,
    but a drifting per-app value would pass it silently.
    """
    seed = _load_seed_module()
    q, t = seed.DEFAULT_QUOTA_MONTHLY, seed.DEFAULT_TOKEN_BUDGET_MONTHLY
    assert q > 0 and t > 0, "the shared defaults must be real caps, not 0/None"
    for app in _load_apps():
        for plan in [app["plan"], *app.get("extra_plans", ())]:
            where = f"{app['key']}/{plan['code']}"
            assert plan["quota_monthly"] == q, f"{where}: quota drifted from the shared default"
            assert plan["token_budget_monthly"] == t, (
                f"{where}: token budget drifted from the shared default"
            )


def test_strata_apps_are_seeded_like_commonpractice() -> None:
    """Migration 067: care-bus, issued by its OWN owner only.

    The commonpractice shape: ``human`` audience, one ``standard`` plan, perpetual,
    metered with the shared default quota / token budget (never unlimited).
    strata-configurator was retired by migration 084 and is no longer declared.
    """
    mod = _load_seed_module()
    seeded = {a["key"]: a for a in mod.APPS}
    assert "strata-configurator" not in seeded  # 084
    for key in ("care-bus",):
        assert key in seeded, f"{key} missing from the app seed"
        app = seeded[key]
        assert tuple(app["issuers"]) == (key,), (key, app["issuers"])
        assert app["audience"] == "human"
        plan = app["plan"]
        assert plan["code"] == "standard"
        assert plan["duration_days"] is None
        assert plan["quota_monthly"] == mod.DEFAULT_QUOTA_MONTHLY
        assert plan["token_budget_monthly"] == mod.DEFAULT_TOKEN_BUDGET_MONTHLY


def test_agent_app_is_seeded_like_the_strata_apps() -> None:
    """Migration 070: agent, issued by its OWN owner only (the strata apps' shape)."""
    mod = _load_seed_module()
    seeded = {a["key"]: a for a in mod.APPS}
    assert "agent" in seeded, "agent missing from the app seed"
    app = seeded["agent"]
    assert tuple(app["issuers"]) == ("agent",), app["issuers"]
    assert app["audience"] == "human"
    plan = app["plan"]
    assert plan["code"] == "standard"
    assert plan["duration_days"] is None
    assert plan["quota_monthly"] == mod.DEFAULT_QUOTA_MONTHLY
    assert plan["token_budget_monthly"] == mod.DEFAULT_TOKEN_BUDGET_MONTHLY


def test_agent_byo_plan_is_seeded_like_commonpractice_byo() -> None:
    """Migration 071: agent issues on `byo` like CP; `standard` stays for existing licences.

    CP's plan-migration creates `byo` ("Bring your own key") with its `standard` plan's
    limits, so agent/byo must carry exactly agent/standard's limits.
    """
    mod = _load_seed_module()
    app = {a["key"]: a for a in mod.APPS}["agent"]
    assert app["plan"]["code"] == "standard", "agent/standard must stay (live licences)"
    extra = {p["code"]: p for p in app.get("extra_plans", ())}
    assert set(extra) == {"byo"}, extra
    byo, std = extra["byo"], app["plan"]
    assert byo["name"] == "Bring your own key"
    limits = (
        "rate_limit_rpm",
        "rate_limit_burst",
        "duration_days",
        "quota_monthly",
        "token_budget_monthly",
    )
    assert {k: byo[k] for k in limits} == {k: std[k] for k in limits}
    assert [p["code"] for p in mod.all_plans(app)] == ["standard", "byo"]
    # Every other app seeds exactly one plan — extra_plans is not a back door.
    assert [a["key"] for a in mod.APPS if a.get("extra_plans")] == ["agent"]
