-- 075: the `speech` app — TTS and ASR as shared PLATFORM services.
--
-- WHY (owner decision 2026-10-22). tts-backend and asr-backend (mini2, exposed at
-- api.nonagentic.ai/tts/ and /asr/) are shared platform services like `vllm` and
-- `laya`, decoupled from Studio (w2v). They get their own licence app key. ONE key for
-- both, because a licence token is per app and agent-api sends one VOICE_TOKEN to both
-- backends.
--
-- WHAT THIS ADDS (data only, no schema) — vllm / laya's shape (scripts/seed_apps.py,
-- the edge-gate block), with an explicit issuer:
--   app          speech "Speech (TTS / ASR)", audience `platform` (a service hop,
--                never offered to a person directly)
--   plan         speech/standard, global, 600 rpm / 60 burst, perpetual,
--                quota 1,000,000 / token budget 50,000,000 (seed_apps.py's
--                DEFAULT_QUOTA_MONTHLY / DEFAULT_TOKEN_BUDGET_MONTHLY; never unlimited)
--   app_issuer   speech -> {platform}: issued by the platform owner only
--                (models.APP_ISSUER_OVERRIDES). Not fr: no fr flow mints it.
--
-- NOT ADDED. No owner (platform exists since 050), no api_client, no bundle, no
-- app_requires edge, no grant. Licences are issued per tenant by the platform
-- (e.g. scripts/provision_service_token.py for svc-agent-api).
--
-- FRESH DATABASE: this file is stamped without running; the app + plan + issuer come
-- from scripts/seed_apps.py (models.APP_ISSUER_OVERRIDES) — pinned equal at head by
-- tests/unit/test_migration_075_speech_platform_app.py.
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/075_speech_platform_app.sql (REFUSES while anything
-- references the app). Roll back models.py and scripts/seed_apps.py too, or the DR
-- seed re-creates the rows.

BEGIN;

INSERT INTO app (key, name, description, audience) VALUES
    (
        'speech',
        'Speech (TTS / ASR)',
        'Speech (TTS / ASR) — access gate for `/tts/` and `/asr/` on api.nonagentic.ai, '
        'enforced at the edge by license-authz (app_key `speech`). Shared platform '
        'service, migration 075.',
        'platform'
    )
ON CONFLICT (key) DO NOTHING;

INSERT INTO plan (
    app_id, code, name, description, region_id, duration_days,
    rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
)
SELECT a.id, 'standard', 'Standard', 'Default per-tenant plan for ' || a.name || '.', NULL,
       NULL, 600, 60, 1000000, 50000000
FROM app a
WHERE a.key = 'speech'
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'standard' AND p.region_id IS NULL
  );

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, 'platform'
FROM app a
WHERE a.key = 'speech'
ON CONFLICT (app_id, owner_key) DO NOTHING;

COMMIT;
