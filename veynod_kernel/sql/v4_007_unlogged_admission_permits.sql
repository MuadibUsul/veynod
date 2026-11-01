-- v4_007: soft admission permits are not durable state.
--
-- admission_permits is reconstructed from the live pipeline and TTL.  Making
-- it UNLOGGED removes a WAL fsync from every claim reservation; a crash simply
-- loses the permits and the pipeline-depth check takes over.  Keep the table
-- and trigger definitions otherwise unchanged.

ALTER TABLE admission_permits SET UNLOGGED;

INSERT INTO schema_migrations (version)
VALUES ('v4_007_unlogged_admission_permits')
ON CONFLICT (version) DO NOTHING;
