-- Downgrade 028. Drops both tables; membership is not recoverable afterwards,
-- which is fine — it is re-derivable from 028 itself.
--
-- ⚠️ The three `raise-my-tickets-*` apps are DELIBERATELY LEFT IN PLACE. Dropping
-- an `app` row cascades to `license`, `plan`, `offering` and `tenant_app_byo_mode`,
-- so if anyone has licensed one of them between the upgrade and this downgrade,
-- dropping it here would silently delete a real entitlement. An unused app row
-- costs nothing; a deleted licence cannot be undone. Remove them by hand only
-- after checking `SELECT count(*) FROM license WHERE app_id = …`.
DROP TABLE IF EXISTS bundle_app;
DROP TABLE IF EXISTS bundle;
