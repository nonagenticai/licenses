-- 039: a tenant HOLDS a bundle, and one licence can speak for the whole bundle.
--
-- ⚠️ SUPERSEDED 2026-09-28 (per-app ruling; no schema change needed). The SCHEMA below
-- stands; the "ONE licence per (tenant, bundle)" and "introspect answers apps[]"
-- semantics do NOT. A bundle is the provisioning/billing object: provisioning now sets
-- bundle_id on ONE per-app licence per (tenant, bundle app), introspect never widens a
-- token beyond its own licence's app, and the bundle-token route takes a required
-- `app`. The column COMMENT below is historical on databases that already ran it.
--
-- WHY (operator ruling 2026-09-27). Every tenant holds a licence BUNDLE that covers
-- every app the conveyor uses. 028 built the catalogue (`bundle`, `bundle_app`) but
-- nothing connects a tenant to a bundle and no licence knows it belongs to one, so
-- "tenant T may call apps A..N" is still N separate app licences. This adds:
--
--   tenant_bundle      which bundles a tenant holds. PRIMARY KEY (tenant_id, bundle_id).
--   license.bundle_id  NULLable. Set on the ONE licence minted per (tenant, bundle)
--                      by scripts/provision_tenant_bundle.py. NULL = a legacy
--                      single-app licence, which behaves exactly as before.
--
-- `/license/introspect` answers `apps: [<keys in the bundle>]` for a licence whose
-- bundle_id is set, alongside the unchanged `app`. Services accept `app ∈ apps[]`.
--
-- ⛔ WHAT THIS DOES NOT DO. It mints nothing, re-points no licence, and gives no
-- tenant a bundle: both new objects are EMPTY after this file. Every existing
-- licence keeps bundle_id NULL and introspects exactly as it did. Metering is
-- unchanged — cost is still counted against the licence's own app.
--
-- CONVEYOR APPS ADDED TO `non-agentic-ai`. The bundle must cover every app the
-- conveyor calls. Of the candidates, only keys that already exist in `app` (seeded
-- by scripts/seed_apps.py or scripts/seed_portal_*.py) are linked, and a key that
-- is absent in this environment is ANNOUNCED AND SKIPPED — the 028 contract. No
-- `app` row is created here.
--   linked if present: kag, keycloak, secrets, licenses, improvement-loop
--   NOT linked — no such app key anywhere in this repo's seeds or migrations:
--     observability, metering, workflows, kaizen, meta-prompt
--   (creating them is a catalogue decision, not a membership one.)
--
-- ON DELETE RESTRICT everywhere: a tenant or bundle that is held, or that a licence
-- names, cannot be deleted out from under that grant.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this file
-- is stamped without running; `models.TenantBundle` and `models.License.bundle_id`
-- declare the same objects under the same names, and tests/unit/test_schema_parity.py
-- measures that the two arms agree. The bundle_app rows below are data and, like
-- 028's, exist only on databases that ran this file.
--
-- SAFETY. A failed migration makes the pod refuse to serve (ext_authz, replicas: 2),
-- so: every CREATE is IF NOT EXISTS, the named FK is pg_constraint-probed, every
-- INSERT is ON CONFLICT DO NOTHING, and a missing app is a NOTICE, never an error.
-- No lock_timeout, matching the rest of this corpus: `license` is ~260 rows and the
-- new column is all-NULL, so the FK validation scan is trivial.
--
-- DOWNGRADE: migrations/downgrades/039_tenant_bundle_and_license_bundle.sql

CREATE TABLE IF NOT EXISTS tenant_bundle (
    tenant_id  UUID NOT NULL,
    bundle_id  UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tenant_bundle_pkey PRIMARY KEY (tenant_id, bundle_id),
    CONSTRAINT fk_tenant_bundle_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE RESTRICT,
    CONSTRAINT fk_tenant_bundle_bundle FOREIGN KEY (bundle_id)
        REFERENCES bundle(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_tenant_bundle_bundle ON tenant_bundle(bundle_id);

ALTER TABLE license ADD COLUMN IF NOT EXISTS bundle_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_license_bundle' AND conrelid = 'license'::regclass
    ) THEN
        ALTER TABLE license
            ADD CONSTRAINT fk_license_bundle
            FOREIGN KEY (bundle_id) REFERENCES bundle(id) ON DELETE RESTRICT;
        RAISE NOTICE '039: fk_license_bundle added';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_license_bundle ON license(bundle_id);

COMMENT ON COLUMN license.bundle_id IS
    'The bundle this ONE licence speaks for (set by scripts/provision_tenant_bundle.py). '
    'NULL = a single-app licence. When set, /license/introspect answers apps[] = the '
    'keys of the bundle''s apps, and a service accepts the call when its app is in apps[].';

DO $$
DECLARE
    r         RECORD;
    v_bundle  UUID;
    v_app     UUID;
    linked    INT := 0;
    skipped   INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('non-agentic-ai', 'kag'),
            ('non-agentic-ai', 'keycloak'),
            ('non-agentic-ai', 'secrets'),
            ('non-agentic-ai', 'licenses'),
            ('non-agentic-ai', 'improvement-loop')
        ) AS t(bundle_key, app_key)
    LOOP
        SELECT id INTO v_bundle FROM bundle WHERE key = r.bundle_key;
        SELECT id INTO v_app    FROM app    WHERE key = r.app_key;

        IF v_bundle IS NULL OR v_app IS NULL THEN
            RAISE NOTICE '039: skipping %/% — %',
                r.bundle_key, r.app_key,
                CASE WHEN v_bundle IS NULL THEN 'no such bundle' ELSE 'no such app' END;
            skipped := skipped + 1;
            CONTINUE;
        END IF;

        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;

    RAISE NOTICE '039: % membership rows ensured, % skipped', linked, skipped;
END $$;

-- Verify
SELECT b.key AS bundle, count(ba.app_id) AS apps
  FROM bundle b
  LEFT JOIN bundle_app ba ON ba.bundle_id = b.id
 WHERE b.key = 'non-agentic-ai'
 GROUP BY b.key;
