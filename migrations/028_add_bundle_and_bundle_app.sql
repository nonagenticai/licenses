-- 028: the registry gains the layer between a tenant and its app licences.
--
-- WHAT IS MISSING. A tenant does not buy an `app`. It buys a BUNDLE —
-- `non-agentic-ai`, `knowledge`, `research`, `merqi`, `raisemytickets`,
-- `commonpractice`, `book-fulfillment`, `w2v` — and the bundle says which app
-- licences that grant mints underneath it. That layer has never existed. There is
-- no table, no column and no API field named bundle anywhere in this repo
-- (grepped 2026-09-04), so every commercial question has had to be answered by
-- reading a list of 47 flat apps and knowing, out of band, which product each one
-- belongs to. `offering` is NOT that layer and cannot become it: it is keyed
-- `ux_offering_app_code`, one row per (app, code), so it is structurally incapable
-- of spanning products.
--
-- WHY MANY-TO-MANY, AND WHY THAT IS THE WHOLE DESIGN. `fr` belongs to
-- `non-agentic-ai`, `knowledge` AND `research` — the operator named all three on
-- 2026-09-04. A `bundle_id` column on `app` therefore cannot express the
-- requirement: it would force a choice the first time the second bundle was
-- seeded, and would do it silently. `bundle_app` is the only shape that holds.
--
-- ⛔ WHAT THIS DOES NOT DO. It does not meter, price, entitle or gate anything.
-- No licence is created, moved, revoked or re-pointed. No existing row of any
-- kind is modified. Consumption stays counted against the individual app licence,
-- because that is the grain at which cost is incurred and at which a microservice
-- can be priced; the bundle sits above that as the commercial/provisioning object.
-- Until something reads these tables, this migration is inert by construction.
--
-- THE THREE NEW APPS. The operator specifies `raisemytickets` as core_app +
-- admin_app + analytics_app, but the catalogue holds ONE flat `raise-my-tickets`
-- app carrying 9 active licences. This file ADDS the three and leaves the flat app
-- and all 9 licences exactly where they are — the bundle contains all four for
-- now. ⚠️ Re-pointing those 9 licences needs someone to say WHICH of the three
-- each customer actually bought. That is business knowledge, it is not in this
-- database, and inventing it would put a wrong answer somewhere durable. Follow-up.
--
-- ⚠️ KEY NAMING IS PROVISIONAL. `app.key` is globally unique, so the bare names
-- `core_app`/`admin_app`/`analytics_app` would claim three very generic keys for
-- one product. They are prefixed here. The uuid is what every FK holds, so a
-- rename later is a one-line UPDATE and costs nothing.
--
-- ⛔ …0002 IS STILL NOT RENAMED, and this file deliberately does not touch it.
-- Migrations 024 and 026 both refused to settle it: fr calls it
-- `workflow-improvement`, this registry calls it `platform-core`, the portal realm
-- retired `gerard-dev` at it, and it carries 20 active licences. That is billing
-- issue #86. NEW EVIDENCE surfaced 2026-09-04 —
-- `n8n_custom_nodes/AA_IMPLEMENTATION/tenant_assets/README.md` maps the
-- `workflow-improvement` slot to …0002, independently of fr — so the tally is now
-- two well-sourced claims for `workflow-improvement` against this registry's
-- label. That STRENGTHENS the case; it does not make it a transcription, and a
-- rename migration is still not entitled to decide it. Recorded here so the next
-- reader has the third source without re-deriving it.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it, because create_all has already built the schema from models.py.
-- That is why `Bundle` and `BundleApp` are added to models.py in the same commit:
-- without the models, a from-scratch database would never get these tables at all
-- and this file would be the only place they exist. The seed data below is
-- therefore ALSO absent on a fresh database — it is a data seed, not schema, and
-- is applied by this file on existing databases only. Seeding a fresh database is
-- `scripts/seed_apps.py`'s job and is out of scope here.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So nothing here may fail on data:
--   * Every CREATE is IF NOT EXISTS.
--   * Every INSERT is guarded ON CONFLICT DO NOTHING, so a re-run is a no-op.
--   * A membership naming an app key that does not exist is ANNOUNCED AND SKIPPED,
--     never fatal. The catalogue differs between environments and a missing app is
--     a normal state, not a broken one.
-- No lock_timeout, matching the rest of this corpus: these are new empty tables
-- and `app` is ~47 rows.
--
-- DOWNGRADE: migrations/downgrades/028_add_bundle_and_bundle_app.sql

CREATE TABLE IF NOT EXISTS bundle (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key         VARCHAR(64) NOT NULL UNIQUE,
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    revoked     BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bundle_app (
    bundle_id  UUID NOT NULL REFERENCES bundle(id) ON DELETE CASCADE,
    app_id     UUID NOT NULL REFERENCES app(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bundle_id, app_id)
);

CREATE INDEX IF NOT EXISTS idx_bundle_app_app ON bundle_app(app_id);

-- The three RaiseMyTickets sub-apps. Additive; the flat `raise-my-tickets` app and
-- its 9 licences are untouched.
INSERT INTO app (key, name, description, audience)
VALUES
    ('raise-my-tickets-core',      'RaiseMyTickets Core App',      'RaiseMyTickets core application',      'human'),
    ('raise-my-tickets-admin',     'RaiseMyTickets Admin App',     'RaiseMyTickets administration application', 'human'),
    ('raise-my-tickets-analytics', 'RaiseMyTickets Analytics App', 'RaiseMyTickets analytics application', 'human')
ON CONFLICT (key) DO NOTHING;

INSERT INTO bundle (key, name, description)
VALUES
    ('non-agentic-ai',   'Non-Agentic AI',   'fr + kanban + n8n and the n8n node modules — the AA system'),
    ('knowledge',        'Knowledge',        'fr + the KAG family'),
    ('research',         'Research',         'fr + the KAG family + n8n'),
    ('merqi',            'Merqi',            'marketing + sales'),
    ('raisemytickets',   'RaiseMyTickets',   'core + admin + analytics applications'),
    ('commonpractice',   'CommonPractice',   'CommonPractice'),
    ('book-fulfillment', 'Book Fulfillment', 'Book fulfillment'),
    ('w2v',              'W2V',              'W2V. ⚠️ carries its own implementation — bundle row seeded, nothing else touched')
ON CONFLICT (key) DO NOTHING;

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
            -- non-agentic-ai: fr + kanban + n8n + the n8n node modules
            ('non-agentic-ai',   'fr'),
            ('non-agentic-ai',   'fr-frontend'),
            ('non-agentic-ai',   'kanban'),
            ('non-agentic-ai',   'n8n'),
            ('non-agentic-ai',   'requirements'),
            ('non-agentic-ai',   'procedures'),
            ('non-agentic-ai',   'coding'),
            ('non-agentic-ai',   'workers'),
            ('non-agentic-ai',   'artifacts'),
            ('non-agentic-ai',   'signal'),
            ('non-agentic-ai',   'tools'),
            ('non-agentic-ai',   'skill-builder'),
            ('non-agentic-ai',   'mcp'),
            -- knowledge: fr + the KAG family
            ('knowledge',        'fr'),
            ('knowledge',        'kag'),
            ('knowledge',        'kag-graph'),
            ('knowledge',        'kag-orchestrator'),
            ('knowledge',        'hirag'),
            ('knowledge',        'karma'),
            ('knowledge',        'ingestion'),
            ('knowledge',        'graph-adapter'),
            ('knowledge',        'nlp-service'),
            -- research: knowledge + n8n
            ('research',         'fr'),
            ('research',         'kag'),
            ('research',         'kag-graph'),
            ('research',         'kag-orchestrator'),
            ('research',         'hirag'),
            ('research',         'karma'),
            ('research',         'ingestion'),
            ('research',         'graph-adapter'),
            ('research',         'nlp-service'),
            ('research',         'n8n'),
            -- merqi
            ('merqi',            'marketing'),
            ('merqi',            'sales'),
            -- raisemytickets: the flat app stays until its 9 licences are re-pointed
            ('raisemytickets',   'raise-my-tickets'),
            ('raisemytickets',   'raise-my-tickets-core'),
            ('raisemytickets',   'raise-my-tickets-admin'),
            ('raisemytickets',   'raise-my-tickets-analytics'),
            -- single-app bundles
            ('commonpractice',   'commonpractice'),
            ('book-fulfillment', 'book-fulfillment'),
            ('w2v',              'w2v'),
            ('w2v',              'w2v-image')
        ) AS t(bundle_key, app_key)
    LOOP
        SELECT id INTO v_bundle FROM bundle WHERE key = r.bundle_key;
        SELECT id INTO v_app    FROM app    WHERE key = r.app_key;

        IF v_bundle IS NULL OR v_app IS NULL THEN
            -- A catalogue that differs between environments is normal, not broken.
            RAISE NOTICE '028: skipping %/% — %',
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

    RAISE NOTICE '028: % membership rows ensured, % skipped', linked, skipped;
END $$;

-- Verify
SELECT b.key AS bundle, count(ba.app_id) AS apps
  FROM bundle b
  LEFT JOIN bundle_app ba ON ba.bundle_id = b.id
 GROUP BY b.key
 ORDER BY b.key;
