-- Migration: Seed the real platform/internal tenants that migration 004 deleted
-- Date: 2026-07-10
-- Description: Migration 004's `DELETE FROM tenants WHERE name = id::text` applied on
-- prod 2026-07-09 and wiped ~39 of 52 tenant rows — every row the backfill had created
-- with name == its own UUID (its fallback name). 013 restored the render/E2E service
-- account; this restores the OTHER real tenants that had no friendly name yet and so
-- were deleted, leaving them to fail-CLOSED to `byo_required` on the token path.
--
-- Scope: ONLY the real tenants that 004 actually deleted and that no other migration
-- owns. Rows that 003/004 seed (n8n-dev, gerard-dev, marketing, sales) or that survived
-- 004 with a friendly name (portal, platform-core, fr-test, book-fulfillment, subhankar…)
-- are intentionally left alone — re-declaring their NAMES here would collide with the
-- 003/004 seeds (which use random uuidv7 ids) on a fresh DB. Unlisted old test tenants
-- are not seeded — they resolve to the `byo_required` default without a row.
--
-- Every name is friendly (never == id::text), so migration 004 can never delete these
-- again. Idempotent + self-asserting: ON CONFLICT re-affirms posture, keeps any name.

INSERT INTO tenants (id, name, byo_mode, created_at) VALUES
    -- fr-test Keycloak-user tenant (the identity the browser-test user carries) → local_allowed
    ('0195f48b-d000-7000-8000-000000000099', 'fr-test-user',  'local_allowed', NOW()),
    -- System / anon-S2S / platform control-plane family → platform_only
    ('00000000-0000-0000-0000-000000000000', 'default-anon',  'platform_only', NOW()),
    ('00000000-0000-0000-0000-000000000001', 'system',        'platform_only', NOW()),
    -- ⚠️ These two ids are NOT anonymous control-plane slots. Each is a real
    -- person's/tenant's canonical uuid7 in the Keycloak PORTAL realm — the literal
    -- `tenant_id` claim inside live user JWTs (see gitops-production
    -- clusters/strato/services/keycloak/realm-portal-configmap.yaml and
    -- worker_management/src/api/tenant_constants.py). They were seeded here on
    -- 2026-07-10 under invented `platform-cp-N` labels because nobody had matched
    -- them to the realm yet; migration 024 renamed them. The names below are the
    -- post-024 truth so a fresh database and a migrated one agree.
    --
    -- The `-portal-realm` suffix is load-bearing: the registry SEPARATELY holds
    -- `n8n-dev` at 019de842-c92a-… , so two rows describe one human tenant. That
    -- duplication is billing issue #86 and is NOT resolved here — this only stops
    -- the registry claiming the uuids belong to nobody.
    ('0195f48b-d000-7000-8000-000000000001', 'n8n-dev-portal-realm', 'platform_only', NOW()),
    ('0195f48b-d000-7000-8000-000000000003', 'george-dev-portal-realm', 'platform_only', NOW()),
    --
    -- ⚠️ …0004 and …0005 are NOT anonymous slots either. They are fr's internal
    -- improvement-loop tenants: fr migrations 062 and 066 INSERT these exact
    -- uuids as the workspaces named `prompt-improvement` and
    -- `procedure-improvement`, fr pins `streams.kind` to them with a CHECK
    -- constraint, and the Keycloak realm-patch script mints them as the
    -- `tenant_id` claim for `system-prompt-operator`, `aa-card-writer-prompt`
    -- and `aa-card-writer-procedure`. 024 left them as `platform-cp-N` because
    -- that evidence had not been found yet; migration 026 renamed them. The
    -- names below are the post-026 truth so a fresh database and a migrated one
    -- agree. (…0002 is still disputed — see models.PLATFORM_TENANTS.)
    ('0195f48b-d000-7000-8000-000000000004', 'prompt-improvement', 'platform_only', NOW()),
    ('0195f48b-d000-7000-8000-000000000005', 'procedure-improvement', 'platform_only', NOW())
ON CONFLICT (id) DO UPDATE
    SET byo_mode = EXCLUDED.byo_mode;  -- re-affirm posture; keep any pre-existing name

-- Verify: the 7 restored tenants, with the expected posture.
SELECT byo_mode, count(*) FROM tenants
WHERE id IN (
    '0195f48b-d000-7000-8000-000000000099',
    '00000000-0000-0000-0000-000000000000','00000000-0000-0000-0000-000000000001',
    '0195f48b-d000-7000-8000-000000000001','0195f48b-d000-7000-8000-000000000003',
    '0195f48b-d000-7000-8000-000000000004','0195f48b-d000-7000-8000-000000000005'
)
GROUP BY byo_mode ORDER BY byo_mode;
