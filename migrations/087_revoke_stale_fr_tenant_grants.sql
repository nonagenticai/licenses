-- 087: REVOKE the stale `tenant_app_grant` rows that let fr act on 33 tenants.
--
-- WHY THIS EXISTS (George 2026-10-31). fr used to be the cluster's ONLY
-- provisioning app: it minted tenants and users for every other service. Now
-- each app provisions its own tenants and users with app-specific licences, so
-- a grant to fr on someone else's tenant no longer reflects anything fr does.
--
-- Migration 050's GRANT BACKFILL (rule 2) created them mechanically: "an owner O
-- gets a grant on tenant T iff O is the SOLE issuer of some app holding an
-- ACTIVE licence on T". fr is the sole issuer of `fr`, `fr-frontend` and `kag`
-- (050's app_issuer seed), and every one of those apps is licensed somewhere, so
-- the rule handed fr a grant on 33 tenants it has never operated.
--
-- They were invisible until #312 made the scoped `/tenants` listing honour
-- grants the way `owner_can_access_tenant` always did. The effect was immediate
-- and measurable: fr's super-admin budgets screen went from the 6 tenants fr
-- owns to 39 — correct per the predicate, wrong as a picture of what fr is.
--
-- ⛔ WHAT IS KEPT, and why it is not the same category. `svc-n8n-local-conveyor`
-- is the conveyor tenant, and fr's super-admin console is the surface George
-- uses to control its budgets and licences (2026-10-31 assignment: "budgets and
-- licences as applicable ONLY to the fr/conveyor"). It stays.
--
-- Revoking is `revoked_at`, never DELETE: the row is the history, and the
-- partial unique index `uq_tenant_app_grant_active` (tenant_id, grantee_owner_key
-- WHERE revoked_at IS NULL) means re-granting later is one INSERT.
--
-- IDEMPOTENT: the predicate is `revoked_at IS NULL`, so a re-run matches nothing.
-- DOWNGRADE: not provided on purpose — un-revoking would restore a reach that
-- no current code path relies on. Re-grant explicitly if that ever changes.

BEGIN;

UPDATE tenant_app_grant
SET revoked_at = now()
WHERE grantee_owner_key = 'fr'
  AND revoked_at IS NULL
  AND tenant_id <> '5f868d42-03f8-51f0-a93c-3e83070dc196'::uuid;

COMMIT;
