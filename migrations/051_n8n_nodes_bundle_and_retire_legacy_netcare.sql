-- 051: the `n8n-nodes` bundle, and the legacy netcare tenant steps aside.
--
-- WHY (George 2026-10-14). netcare becomes a FRESH CommonPractice tenant: a new id,
-- created through CP's own API (PUT /tenants/{id}), holding every customer-facing
-- app plus the six n8n node-module apps, delivered through licence BUNDLES that the
-- owner (CP) ensures (POST /internal/tenants/{id}/bundles/{bundle}/ensure). No
-- break-glass. Two things stand in the way and this file removes both:
--
-- A. THE SIX NODE-MODULE APPS ARE IN NO BUNDLE. kaizen_core_actions,
--    kaizen_discover_actions, meta_prompt_core_actions, meta_prompt_discover_actions,
--    metering, observability (the n8n node modules that carry an app key; modules
--    without one are internal and skipped). 039 recorded them as "NOT linked — no
--    such app key"; they exist now. New bundle `n8n-nodes`, same 028/039/040 shape:
--    a bundle row plus bundle_app rows, a missing app ANNOUNCED AND SKIPPED.
--
--    AND THEY HAVE NO PLAN A BUNDLE CAN USE. Bundle ensure picks, per app, the FIRST
--    code of the caller's `plans` list that exists (non-revoked) on that app
--    (services/bundle_ensure.choose_plan); it never invents one. netcare's 33
--    bundle licences all sit on `tenant-pooled` (quota 1,000,000, token budget NULL:
--    usage draws only on the tenant pool). Measured read-only on the primary 2026-10-14,
--    the six apps have NO `tenant-pooled` plan, and two have no plan at all:
--      kaizen_core_actions           (none)
--      kaizen_discover_actions       system-workflow
--      meta_prompt_core_actions      (none)
--      meta_prompt_discover_actions  system-prompt
--      metering                      portal-n8n-dev
--      observability                 perpetual
--    so `plans: ["tenant-pooled"]` would REFUSE `no_plan` for all six, and no list
--    at all could cover the two plan-less apps. This file adds a `tenant-pooled`
--    plan to each of the six that lacks one, with the exact name / description /
--    quota / budget shape the existing 33 carry; rate limits are the app's own
--    highest existing plan's, else the column defaults (60 / 10). An app that has
--    a REVOKED `tenant-pooled` is left alone (retiring it was a decision) and named.
--
-- B. THE NAME `netcare` IS TAKEN IN CP's NAMESPACE. 050 gave the old netcare tenant
--    (01a0ef72-31af-7561-936a-b8383b5fb6c8, archived 2026-10-12) to commonpractice,
--    so UNIQUE(owner_key, name) refuses CP a fresh `netcare`. It is retired, by ID:
--      name       netcare -> netcare-legacy
--      owner_key  commonpractice -> platform  (it is nobody's live customer now)
--      archived_at  kept
--      grants     every active tenant_app_grant on it revoked (revoked_at = now())
--    Its licences, tenant_bundle rows, subjects and tokens are NOT touched: the
--    archive already makes them inert (an archived tenant mints no token and gates
--    no spend). A no-op when the row is absent (every non-prod database) or already
--    renamed. If (platform, netcare-legacy) is somehow taken the retirement is
--    SKIPPED with a NOTICE — a failed migration makes the pod refuse to serve.
--
-- ⛔ WHAT THIS DOES NOT DO. It mints nothing, creates no tenant, gives no tenant a
-- bundle. CP creates the new netcare and ensures `n8n-nodes` (+ the customer-facing
-- bundles) through the API, with `plans: ["tenant-pooled"]`.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). Like 028/039/040 this is DATA
-- ONLY: on a fresh database it is stamped without running, and the bundle / plans
-- exist only where it ran. No schema object changes, so the schema-parity arms are
-- unchanged.
--
-- SAFETY. ONE transaction (BEGIN/COMMIT run as statements, as in 050). Every INSERT
-- is ON CONFLICT DO NOTHING or NOT EXISTS-guarded; the retirement is guarded by id
-- and current name; a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/051_n8n_nodes_bundle_and_retire_legacy_netcare.sql

BEGIN;

INSERT INTO bundle (key, name, description)
VALUES (
    'n8n-nodes',
    'n8n node modules',
    'The n8n node-module apps that carry an app key: kaizen + meta-prompt actions, '
    'metering, observability (George 2026-10-14)'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    r         RECORD;
    v_bundle  UUID;
    v_app     UUID;
    linked    INT := 0;
    skipped   INT := 0;
    planned   INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'n8n-nodes';
    FOR r IN
        SELECT * FROM (VALUES
            ('kaizen_core_actions'),
            ('kaizen_discover_actions'),
            ('meta_prompt_core_actions'),
            ('meta_prompt_discover_actions'),
            ('metering'),
            ('observability')
        ) AS t(app_key)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        IF v_app IS NULL THEN
            -- A catalogue that differs between environments is normal, not broken.
            RAISE NOTICE '051: skipping n8n-nodes/% — no such app', r.app_key;
            skipped := skipped + 1;
            CONTINUE;
        END IF;

        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;

        IF EXISTS (
            SELECT 1 FROM plan
             WHERE app_id = v_app AND code = 'tenant-pooled' AND region_id IS NULL
        ) THEN
            IF EXISTS (
                SELECT 1 FROM plan
                 WHERE app_id = v_app AND code = 'tenant-pooled' AND region_id IS NULL
                   AND revoked
            ) THEN
                RAISE NOTICE '051: % has a REVOKED tenant-pooled plan — left alone', r.app_key;
            END IF;
            CONTINUE;
        END IF;

        INSERT INTO plan (
            app_id, code, name, description,
            rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
        )
        SELECT
            v_app,
            'tenant-pooled',
            'Tenant pooled (tenant budget)',
            'Tenant-level licence with NO per-licence token budget; usage draws only '
            'from the tenant pool budget. Created by migration 051 for the n8n-nodes bundle.',
            COALESCE(max(p.rate_limit_rpm), 60),
            COALESCE(max(p.rate_limit_burst), 10),
            1000000,
            NULL
          FROM plan p
         WHERE p.app_id = v_app AND NOT p.revoked;
        planned := planned + 1;
    END LOOP;

    RAISE NOTICE '051: % n8n-nodes membership rows ensured, % skipped, % tenant-pooled plans added',
        linked, skipped, planned;
END $$;

DO $$
DECLARE
    v_name     TEXT;
    v_revoked  INT;
BEGIN
    SELECT name INTO v_name FROM tenants
     WHERE id = '01a0ef72-31af-7561-936a-b8383b5fb6c8';
    IF v_name IS NULL THEN
        RAISE NOTICE '051: legacy netcare tenant absent — nothing to retire';
        RETURN;
    END IF;
    IF v_name <> 'netcare' THEN
        RAISE NOTICE '051: legacy netcare tenant is already named % — nothing to retire', v_name;
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM tenants
         WHERE owner_key = 'platform' AND name = 'netcare-legacy'
           AND id <> '01a0ef72-31af-7561-936a-b8383b5fb6c8'
    ) THEN
        RAISE NOTICE '051: (platform, netcare-legacy) is taken by another tenant — legacy netcare NOT retired';
        RETURN;
    END IF;

    UPDATE tenants
       SET name = 'netcare-legacy', owner_key = 'platform'
     WHERE id = '01a0ef72-31af-7561-936a-b8383b5fb6c8';

    UPDATE tenant_app_grant
       SET revoked_at = now()
     WHERE tenant_id = '01a0ef72-31af-7561-936a-b8383b5fb6c8'
       AND revoked_at IS NULL;
    GET DIAGNOSTICS v_revoked = ROW_COUNT;

    RAISE NOTICE '051: legacy netcare retired as netcare-legacy (platform); % grant(s) revoked',
        v_revoked;
END $$;

COMMIT;
