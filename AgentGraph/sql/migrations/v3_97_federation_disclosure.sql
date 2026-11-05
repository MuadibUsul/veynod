-- v3.97 Directory disclosure settings (task P4-4, docs/federation/PRIVACY.md).
--
-- The owner of a listed workspace chooses which reputation measures the
-- directory shows to others. Governance outcomes (sanctions in force and
-- resolved complaints) are always shown: safety information is not opt-out.

ALTER TABLE federation_directory_listings
    ADD COLUMN IF NOT EXISTS disclosure JSONB NOT NULL DEFAULT
        '{"completion": true, "acceptance": true, "response": true, "skills": true,
          "cooperation": true}'::jsonb;
ALTER TABLE federation_directory_listings DROP CONSTRAINT IF EXISTS
    federation_directory_listings_disclosure_check;
ALTER TABLE federation_directory_listings ADD CONSTRAINT
    federation_directory_listings_disclosure_check CHECK (jsonb_typeof(disclosure) = 'object');
