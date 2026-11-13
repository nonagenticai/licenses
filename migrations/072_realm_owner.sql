-- 072: realm_owner — Keycloak realm -> the app (tenant_owner) that owns its users.
--
-- WHY (owner decision 2026-10-20, realm-per-app design §2/§3 P0/§4 item 7). Each app
-- moves to its own Keycloak realm; CommonPractice first. A realm admin of app A can set
-- any `tenant_id` on tokens of A's realm, so without this table a token from A's realm
-- could name a tenant of app B (cross-realm tenant forgery). With a row here, a verified
-- token from that realm may act only on tenants the realm's owner OWNS or holds an
-- ACTIVE tenant_app_grant on; otherwise 403 `realm_owner_mismatch`. A realm with NO row
-- keeps the pre-072 behaviour.
--
-- SEED: only ('commonpractice', 'commonpractice'). portal and secure-apps are
-- deliberately NOT seeded (portal serves every app's users today; secure-apps is S2S).
-- No user-visible change: nothing presents commonpractice-realm tokens until gitops adds
-- that realm to KEYCLOAK_TRUSTED_ISSUERS / USER_TRUSTED_ISSUERS.
--
-- FRESH DATABASE: this file is stamped without running; the table comes from
-- models.RealmOwner and the row from models.REALM_OWNERS via the boot seed
-- (seed_ownership_reference) — pinned equal by tests/unit/test_migration_072_realm_owner.py.
--
-- ONE TRANSACTION. IF NOT EXISTS / ON CONFLICT DO NOTHING: a re-run is a no-op and an
-- operator's row is never overwritten.
--
-- DOWNGRADE: migrations/downgrades/072_realm_owner.sql (drops the table).

BEGIN;

CREATE TABLE IF NOT EXISTS realm_owner (
    realm VARCHAR(255) NOT NULL,
    owner_key VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT realm_owner_pkey PRIMARY KEY (realm),
    CONSTRAINT fk_realm_owner_owner FOREIGN KEY (owner_key)
        REFERENCES tenant_owner (key) ON DELETE RESTRICT
);

INSERT INTO realm_owner (realm, owner_key) VALUES
    ('commonpractice', 'commonpractice')
ON CONFLICT (realm) DO NOTHING;

COMMIT;
