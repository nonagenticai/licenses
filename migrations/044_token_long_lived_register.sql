-- 044: the long-lived token register (licenses#244).
--
-- WHY. Measured 2026-10-02: 199 active perpetual tokens (127 api, 72 internal) and no
-- mint path that ever set an expiry. Ruling (option 2): a NEW api mint without an expiry
-- gets a bounded default (TOKEN_API_DEFAULT_EXPIRY_DAYS, 365), and a NEW token may be
-- perpetual ONLY as a registered, owned long-lived token: internal/node tokens, and the
-- n8n licence-token path (api on LONG_LIVED_API_TENANTS). The policy is enforced at mint
-- time in DatabaseManager.create_token; these columns are the register it writes.
--
--   long_lived_owner   who owns the perpetual credential
--   long_lived_reason  why it may be perpetual
--
-- ck_token_long_lived_registered: a registered row names BOTH, is perpetual, and carries
-- no role. Both NULL is every ordinary token.
--
-- ⛔ EXISTING TOKENS ARE UNTOUCHED. No backfill and no revocation: the 199 legacy
-- perpetual rows stay perpetual and UNREGISTERED (both columns NULL), so the CHECK holds
-- for every existing row by construction and introspection is unchanged. Their lifecycle
-- is decided on 2026-12-01, not here.
--
-- Idempotent: `ADD COLUMN IF NOT EXISTS` + `pg_constraint` probe.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py): on a FRESH database this file is
-- stamped without running; `models.Token` declares both columns and the constraint, and
-- tests/unit/test_schema_parity.py measures that the two arms agree.
--
-- DOWNGRADE: migrations/downgrades/044_token_long_lived_register.sql

ALTER TABLE token ADD COLUMN IF NOT EXISTS long_lived_owner text;
ALTER TABLE token ADD COLUMN IF NOT EXISTS long_lived_reason text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_token_long_lived_registered' AND conrelid = 'token'::regclass
    ) THEN
        ALTER TABLE token
            ADD CONSTRAINT ck_token_long_lived_registered
            CHECK (
                (long_lived_owner IS NULL AND long_lived_reason IS NULL)
                OR (
                    expires_at IS NULL
                    AND cardinality(granted_roles) = 0
                    AND length(btrim(long_lived_owner)) > 0
                    AND length(btrim(long_lived_reason)) > 0
                )
            );
        RAISE NOTICE '044: ck_token_long_lived_registered added';
    END IF;
END
$$;
