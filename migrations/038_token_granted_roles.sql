-- 038: `token.granted_roles` — the ONLY place a licence token can carry a role.
--
-- WHY. ingestion_service's operator purge (`DELETE /ingest/documents/{id}?scope=document`)
-- is gated on `platform-admin` read from the introspection of the caller's licence, and
-- `/license/introspect` has never returned a role: licences had no role concept at all, so
-- the operator route was unreachable and the only thing that could open it was a
-- co-holder fallback that let one user destroy another's PHI.
--
-- WHERE THE ROLE COMES FROM. Not from here. The authority is the Keycloak realm role
-- `platform-admin` on the operator's OWN verified JWT, checked at MINT time by
-- `POST /licenses/{id}/operator-token` (which also requires the licence's subject to be the
-- caller). This column only RECORDS that grant on the one token minted for it, so the auth
-- hot path (introspect) never has to call Keycloak.
--
-- ⛔ TWO CONSTRAINTS MAKE A MIS-MINT UNREPRESENTABLE, not merely unlikely:
--   ck_token_granted_roles_allowlist   the only grantable role is `platform-admin`.
--   ck_token_granted_roles_short_lived a token WITH a role must expire, and no later
--                                      than created_at + 65 min (the route caps at 60;
--                                      5 min absorbs app-vs-DB clock skew). A perpetual
--                                      or long-lived admin credential cannot be stored.
--
-- BACKWARD COMPATIBLE. `NOT NULL DEFAULT '{}'`: every existing token gets `{}` (no role),
-- so both CHECKs hold for every existing row by construction and introspection answers
-- `roles: []` for all of them. Code rolled back to before this change never writes the
-- column, so it keeps getting `{}` and cannot violate either CHECK.
--
-- Idempotent: `ADD COLUMN IF NOT EXISTS` + `pg_constraint` probes.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py): on a FRESH database this file is
-- stamped without running; `models.Token` declares the column and both constraints, and
-- tests/unit/test_schema_parity.py measures that the two arms agree.
--
-- DOWNGRADE: migrations/downgrades/038_token_granted_roles.sql

ALTER TABLE token ADD COLUMN IF NOT EXISTS granted_roles text[] NOT NULL DEFAULT '{}'::text[];

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_token_granted_roles_allowlist' AND conrelid = 'token'::regclass
    ) THEN
        ALTER TABLE token
            ADD CONSTRAINT ck_token_granted_roles_allowlist
            CHECK (granted_roles <@ ARRAY['platform-admin']::text[]);
        RAISE NOTICE '038: ck_token_granted_roles_allowlist added';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_token_granted_roles_short_lived' AND conrelid = 'token'::regclass
    ) THEN
        ALTER TABLE token
            ADD CONSTRAINT ck_token_granted_roles_short_lived
            CHECK (
                cardinality(granted_roles) = 0
                OR (expires_at IS NOT NULL AND expires_at <= created_at + interval '65 minutes')
            );
        RAISE NOTICE '038: ck_token_granted_roles_short_lived added';
    END IF;
END $$;

COMMENT ON COLUMN token.granted_roles IS
    'Roles this ONE token carries on /license/introspect. Empty for every ordinary token. '
    'Non-empty only for a short-lived operator token minted by POST '
    '/licenses/{id}/operator-token for a caller whose own Keycloak JWT held the role.';
