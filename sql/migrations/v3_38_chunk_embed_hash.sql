-- v3.38 Incremental re-embedding (R6, 2026-10-24 playtest cost item).
--
-- A chunk keeps the hash of the exact text it embedded (title, heading and
-- body) together with the model and dimensions, so a new version of a large
-- document reuses the vectors of the chunks it did not change and pays only for
-- the ones it did. Existing rows have no hash: their next version embeds in full
-- once, as before, and is incremental from then on.

ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embed_hash VARCHAR(64);
