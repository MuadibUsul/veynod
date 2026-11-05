-- v3.96 Reputation integrity: same-controller links and pair reviews
-- (task P4-3, docs/federation/ICD_P4_REPUTATION.md §10).
--
-- Workspaces under one subject are related by construction. Operators can
-- also link identities they found to share a controller, and review a pair
-- the system flagged (genuine restores its weight; collusive keeps it at
-- zero and links the two).

CREATE TABLE IF NOT EXISTS federation_controller_links (
    fid_a VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    fid_b VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    reason TEXT NOT NULL CHECK (char_length(reason) BETWEEN 5 AND 1000),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fid_a, fid_b),
    CHECK (fid_a < fid_b)
);

CREATE TABLE IF NOT EXISTS federation_pair_reviews (
    requester_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    provider_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    verdict VARCHAR(12) NOT NULL CHECK (verdict IN ('genuine', 'collusive')),
    note TEXT NOT NULL CHECK (char_length(note) BETWEEN 5 AND 1000),
    reviewed_by TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (requester_fid, provider_fid)
);

REVOKE ALL ON federation_controller_links FROM agentgraph_app;
REVOKE ALL ON federation_pair_reviews FROM agentgraph_app;
