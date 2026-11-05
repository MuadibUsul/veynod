-- v3.89 Federation between independent deployments (task F-11, ICD_I_I2I.md).
--
-- Where another deployment's workspace is reached. Registered by this
-- deployment's operator; a peer's identity is imported (public keys only,
-- hosted = false) after its self-certifying id and record signatures check.

CREATE TABLE IF NOT EXISTS federation_remote_endpoints (
    federation_id VARCHAR(64) PRIMARY KEY REFERENCES federation_identities(federation_id)
        CHECK (federation_id ~ '^fed:workspace:[a-z2-7]{26}$'),
    base_url TEXT NOT NULL CHECK (base_url ~ '^https?://' AND char_length(base_url) <= 300),
    registered_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refreshed_at TIMESTAMPTZ
);
REVOKE ALL ON federation_remote_endpoints FROM agentgraph_app;
