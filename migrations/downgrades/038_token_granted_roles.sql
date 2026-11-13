-- Downgrade 038. Drops `token.granted_roles` and its two CHECK constraints.
--
-- ⚠️ Rolls back the CODE first: a tree that still has the operator-token route and the
-- introspect `roles` read would 500 on UndefinedColumn once this runs.
--
-- Lossy only for LIVE operator tokens: they keep authenticating but lose their role
-- (introspection answers `roles: []`), i.e. the downgrade fails CLOSED. Such tokens expire
-- within 65 minutes of creation by construction, so after that window nothing is lost.
ALTER TABLE token DROP CONSTRAINT IF EXISTS ck_token_granted_roles_short_lived;
ALTER TABLE token DROP CONSTRAINT IF EXISTS ck_token_granted_roles_allowlist;
ALTER TABLE token DROP COLUMN IF EXISTS granted_roles;
