-- MAGI Initial Schema Migration
-- This migration represents the baseline schema from setup_magi_brain.sql + init_auth.sql.
-- For existing installations, mark this as applied without running:
--   INSERT INTO magi_schema_versions (version, description) VALUES ('001', 'initial schema');

-- UP
-- Note: The actual initial schema is defined in setup_magi_brain.sql and init_auth.sql.
-- This migration serves as a baseline marker for the migration framework.
-- New installations should run setup_magi_brain.sql first, then mark this as applied.
SELECT 1;

-- DOWN
-- Cannot rollback initial schema — this is the baseline.
SELECT 1;
