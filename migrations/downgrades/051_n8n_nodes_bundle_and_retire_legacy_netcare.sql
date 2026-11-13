-- Downgrade 051. Puts the legacy netcare tenant back and removes the `n8n-nodes`
-- bundle and the `tenant-pooled` plans 051 added — but ONLY while nothing uses them.
--
-- ⛔ REFUSES (raises, changes nothing) when:
--   * any licence names the n8n-nodes bundle or any tenant holds it (039: ON DELETE
--     RESTRICT; un-provisioning a grant is an operator decision, not a rollback's);
--   * any licence sits on a plan 051 created (license.plan_id ON DELETE RESTRICT);
--   * commonpractice already has a tenant named `netcare` other than the legacy row
--     (the fresh CP netcare this migration made room for): rename one first.
--
-- GRANTS. 051 revoked every active grant on the legacy tenant. This re-activates,
-- per grantee, the MOST RECENTLY revoked grant where no active one exists — the
-- pre-051 state, since every grant on that tenant was active before 051 ran.
--
-- The plans 051 created are found by their description ("Created by migration 051"),
-- so a `tenant-pooled` plan that existed before 051 is never touched.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_licence  INT;
    n_holder   INT;
    n_planned  INT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'n8n-nodes';
    IF v_bundle IS NOT NULL THEN
        SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
        SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
        IF n_licence > 0 OR n_holder > 0 THEN
            RAISE EXCEPTION '051 downgrade REFUSED: bundle n8n-nodes is held (% licence(s) name it, % tenant_bundle row(s)). Un-provision those grants first; nothing was changed.',
                n_licence, n_holder;
        END IF;
    END IF;

    SELECT count(*) INTO n_planned
      FROM license l JOIN plan p ON p.id = l.plan_id
     WHERE p.code = 'tenant-pooled' AND p.description LIKE '%Created by migration 051%';
    IF n_planned > 0 THEN
        RAISE EXCEPTION '051 downgrade REFUSED: % licence(s) sit on a tenant-pooled plan 051 created; nothing was changed.',
            n_planned;
    END IF;

    IF EXISTS (
        SELECT 1 FROM tenants
         WHERE owner_key = 'commonpractice' AND name = 'netcare'
           AND id <> '01a0ef72-31af-7561-936a-b8383b5fb6c8'
    ) AND EXISTS (
        SELECT 1 FROM tenants
         WHERE id = '01a0ef72-31af-7561-936a-b8383b5fb6c8' AND name = 'netcare-legacy'
    ) THEN
        RAISE EXCEPTION '051 downgrade REFUSED: commonpractice already has another tenant named netcare; rename it before restoring the legacy one. Nothing was changed.';
    END IF;

    IF v_bundle IS NOT NULL THEN
        DELETE FROM bundle_app WHERE bundle_id = v_bundle;
        DELETE FROM bundle WHERE id = v_bundle;
    END IF;
    DELETE FROM plan
     WHERE code = 'tenant-pooled' AND description LIKE '%Created by migration 051%';
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM tenants
         WHERE id = '01a0ef72-31af-7561-936a-b8383b5fb6c8' AND name = 'netcare-legacy'
    ) THEN
        RAISE NOTICE '051 downgrade: legacy netcare not retired here — nothing to restore';
        RETURN;
    END IF;

    UPDATE tenants
       SET name = 'netcare', owner_key = 'commonpractice'
     WHERE id = '01a0ef72-31af-7561-936a-b8383b5fb6c8';

    UPDATE tenant_app_grant g
       SET revoked_at = NULL
     WHERE g.id IN (
        SELECT DISTINCT ON (r.grantee_owner_key) r.id
          FROM tenant_app_grant r
         WHERE r.tenant_id = '01a0ef72-31af-7561-936a-b8383b5fb6c8'
           AND r.revoked_at IS NOT NULL
           AND NOT EXISTS (
                SELECT 1 FROM tenant_app_grant a
                 WHERE a.tenant_id = r.tenant_id
                   AND a.grantee_owner_key = r.grantee_owner_key
                   AND a.revoked_at IS NULL
           )
         ORDER BY r.grantee_owner_key, r.revoked_at DESC
     );
END $$;

-- Un-stamp 051 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '051_n8n_nodes_bundle_and_retire_legacy_netcare.sql';
    END IF;
END $$;

COMMIT;
