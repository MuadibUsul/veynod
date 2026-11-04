-- v3.85 Federation delegations: automatic delivery retries, dead letters and
-- cancellation requests (federation diagram F-04 for delegations, F-08c).
--
-- Contract: docs/federation/ICD_I_BRIDGE.md §3 and §5, docs/federation/ICD_I_MSG.md §4.1.
-- A cancellation request is a flag on both copies, not a step in the version
-- chain: the requester may ask at any time, the provider confirms with a
-- versioned delegate.cancel_ack, and until then the work may still deliver
-- (cancellation_policy 'drain').

ALTER TABLE federation_delegations
    ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (delivery_attempts >= 0),
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_delivery_error VARCHAR(64),
    ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_requested_by TEXT,
    ADD COLUMN IF NOT EXISTS cancel_reason TEXT;

-- What the delivery loop scans: queued objects that are due and not dead.
CREATE INDEX IF NOT EXISTS idx_federation_delegations_retry_due
    ON federation_delegations (next_retry_at)
    WHERE dead_lettered_at IS NULL AND next_retry_at IS NOT NULL;
