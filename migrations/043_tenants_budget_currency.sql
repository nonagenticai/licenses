-- 043: tenants.budget_currency — a tenant whose budget is MONEY (tenant-inference-
-- provisioning PLAN §2.2, gate 1a).
--
-- WHY. Under the EUR plan the owner sets a tenant's budget as an integer EUR
-- amount, held by billing (tenant_money_budget). Billing POOLS license_sync TOKEN
-- budgets per tenant (budget_debit._select_active_budgets), so a token budget this
-- service pushed for the same tenant would be a second, unrelated cap. With this
-- column set, _provision_budget and the nightly licenses-metering-reconcile skip
-- the TOKEN leg for the tenant (the request QUOTA is still pushed).
--
-- VALUES. NULL (every existing tenant; the token-budget behaviour is unchanged) or
-- one of 'EUR', 'USD', 'CAD' (PLAN REV F, owner ruling 2026-10-02: multi-currency
-- now). NULL -> a currency is a license-admin call; switching to a DIFFERENT
-- currency is platform-admin only and audited (409 budget_currency_immutable for
-- anyone else). Clearing back to NULL is never allowed.
--
-- SAFETY. ADD COLUMN ... NULL with no default is catalogue-only (no rewrite, no row
-- touched). IF NOT EXISTS makes a re-run a no-op; the inline CHECK is skipped with
-- the column on a re-run, so it cannot be added twice.
--
-- DOWNGRADE: migrations/downgrades/043_tenants_budget_currency.sql (drops the column;
-- loses every currency mark, so roll the code back with it).

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS budget_currency char(3) NULL
        CONSTRAINT ck_tenants_budget_currency CHECK (budget_currency IN ('EUR', 'USD', 'CAD'));
