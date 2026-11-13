-- Downgrade 047: drop subject.actor_sub.
--
-- ⚠️ LOSSY: every stated actor is gone and licence-token-mode metering events lose
-- their actor again. Roll back the CODE as well (models.Subject.actor_sub, the
-- introspect `subject_actor_sub` field), or the schema-parity gate reports the arms
-- diverging and introspection reads a column that no longer exists.
ALTER TABLE subject
    DROP COLUMN IF EXISTS actor_sub;
