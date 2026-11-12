-- v3.99 Federation resource rights (simulated) and the Agent Poker Arena
-- (task fed-resource-rights-poker-arena-sim-v1, R1-R3;
-- docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md).
--
-- NO ECONOMIC VALUE. DEMO_M is a demonstration resource right: it cannot be
-- bought, withdrawn, exchanged for SIM or anything else, or used to pay a
-- real third-party model. ARENA_CHIP has no ledger at all: chips live only in
-- a match's engine state. Real resource rights (R4) and value-staked games
-- (R5) are separate owner and legal gates and are not built here.
--
-- The DEMO_M ledger is its own pair of tables, separate from the SIM ledger
-- (v3.98): no transaction can touch both, and the unit column only admits
-- DEMO_M.

-- What a workspace declares it can serve. Only MOCK_DEMO is escrowable;
-- a self-reported subscription is display-only and never backs a voucher.
CREATE TABLE IF NOT EXISTS federation_rr_specs (
    spec_id VARCHAR(40) PRIMARY KEY CHECK (spec_id ~ '^frs_[a-z2-7]{26}$'),
    provider_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    resource_class VARCHAR(32) NOT NULL CHECK (resource_class IN (
        'MOCK_DEMO', 'SELF_REPORTED_SUBSCRIPTION',
        'CONTROLLED_PREPAID', 'AUTHORIZED_PROVIDER_SERVICE')),
    escrowable BOOLEAN NOT NULL,
    unit VARCHAR(8) NOT NULL DEFAULT 'DEMO_M' CHECK (unit = 'DEMO_M'),
    model_family TEXT NOT NULL CHECK (char_length(model_family) BETWEEN 1 AND 80),
    allowed_use TEXT NOT NULL CHECK (char_length(allowed_use) BETWEEN 1 AND 500),
    license_basis_ref TEXT CHECK (license_basis_ref IS NULL OR char_length(license_basis_ref) <= 500),
    measured_source VARCHAR(16) NOT NULL
        CHECK (measured_source IN ('mock', 'self_reported', 'adapter', 'platform')),
    confidence VARCHAR(8) NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    declared_tokens BIGINT NOT NULL CHECK (declared_tokens > 0),
    max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrency BETWEEN 1 AND 100),
    revocation_policy TEXT NOT NULL,
    fulfilment_policy TEXT NOT NULL,
    spec_hash VARCHAR(64) NOT NULL,
    state VARCHAR(8) NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'REVOKED')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- RR-02 in the schema: only the simulated class can ever be escrowed.
    CHECK (escrowable = (resource_class = 'MOCK_DEMO'))
);
CREATE INDEX IF NOT EXISTS idx_federation_rr_specs_provider
    ON federation_rr_specs (provider_fid, state);

-- DEMO_M ledger. Accounts:
--   demo_mint                  where simulated capacity comes from (may go negative)
--   cap:<spec>                 a provider's unreserved simulated capacity
--   voucher:<voucher>          a beneficiary's unused right on a provider
--   resv:<reservation>         held for one delegation
--   used:<spec>                consumed by delivered work
--   stake:<match>:<seat>       an arena stake, held while the match runs
CREATE TABLE IF NOT EXISTS federation_rr_txns (
    txn_id VARCHAR(40) PRIMARY KEY CHECK (txn_id ~ '^frx_[a-z2-7]{26}$'),
    kind VARCHAR(16) NOT NULL CHECK (kind IN (
        'capacity', 'issue', 'reserve', 'consume', 'release', 'expire', 'revoke', 'waive',
        'stake_hold', 'stake_award', 'stake_return', 'compensate')),
    unit VARCHAR(8) NOT NULL DEFAULT 'DEMO_M' CHECK (unit = 'DEMO_M'),
    reference TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS federation_rr_entries (
    id BIGSERIAL PRIMARY KEY,
    txn_id VARCHAR(40) NOT NULL REFERENCES federation_rr_txns(txn_id),
    account TEXT NOT NULL CHECK (account ~ ('^(demo_mint|cap:frs_[a-z2-7]{26}|used:frs_[a-z2-7]{26}'
        '|voucher:frv_[a-z2-7]{26}|resv:frr_[a-z2-7]{26}|stake:fam_[a-z2-7]{26}:[01])$')),
    unit VARCHAR(8) NOT NULL DEFAULT 'DEMO_M' CHECK (unit = 'DEMO_M'),
    amount BIGINT NOT NULL CHECK (amount <> 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_rr_entries_account
    ON federation_rr_entries (account, id);

CREATE OR REPLACE FUNCTION federation_rr_balanced() RETURNS trigger AS $$
DECLARE
    total BIGINT;
BEGIN
    SELECT COALESCE(sum(amount), 0) INTO total FROM federation_rr_entries
     WHERE txn_id = NEW.txn_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'DEMO_M transaction % does not balance (%)', NEW.txn_id, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_rr_balanced ON federation_rr_entries;
CREATE CONSTRAINT TRIGGER trg_federation_rr_balanced
    AFTER INSERT ON federation_rr_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION federation_rr_balanced();

-- No account but demo_mint ever goes below zero (no overdraft, no double spend).
CREATE OR REPLACE FUNCTION federation_rr_no_overdraft() RETURNS trigger AS $$
DECLARE
    total BIGINT;
BEGIN
    IF NEW.account = 'demo_mint' THEN
        RETURN NULL;
    END IF;
    SELECT COALESCE(sum(amount), 0) INTO total FROM federation_rr_entries
     WHERE account = NEW.account;
    IF total < 0 THEN
        RAISE EXCEPTION 'DEMO_M account % would go below zero (%)', NEW.account, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_rr_no_overdraft ON federation_rr_entries;
CREATE CONSTRAINT TRIGGER trg_federation_rr_no_overdraft
    AFTER INSERT ON federation_rr_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION federation_rr_no_overdraft();

CREATE OR REPLACE FUNCTION federation_rr_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the DEMO_M ledger is append-only' USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_rr_entries_guard ON federation_rr_entries;
CREATE TRIGGER trg_federation_rr_entries_guard BEFORE UPDATE OR DELETE
    ON federation_rr_entries FOR EACH ROW EXECUTE FUNCTION federation_rr_append_only();
DROP TRIGGER IF EXISTS trg_federation_rr_txns_guard ON federation_rr_txns;
CREATE TRIGGER trg_federation_rr_txns_guard BEFORE UPDATE OR DELETE
    ON federation_rr_txns FOR EACH ROW EXECUTE FUNCTION federation_rr_append_only();

-- A bounded right of one workspace to request service from another. It
-- never carries the issuer's account, key or tool access.
CREATE TABLE IF NOT EXISTS federation_rr_vouchers (
    voucher_id VARCHAR(40) PRIMARY KEY CHECK (voucher_id ~ '^frv_[a-z2-7]{26}$'),
    spec_id VARCHAR(40) NOT NULL REFERENCES federation_rr_specs(spec_id),
    issuer_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    beneficiary_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    origin VARCHAR(12) NOT NULL CHECK (origin IN ('grant', 'arena', 'compensate')),
    origin_ref TEXT,
    unit VARCHAR(8) NOT NULL DEFAULT 'DEMO_M' CHECK (unit = 'DEMO_M'),
    face_amount BIGINT NOT NULL CHECK (face_amount > 0),
    usage_scope TEXT NOT NULL,
    legal_basis_status VARCHAR(16) NOT NULL DEFAULT 'demo_only'
        CHECK (legal_basis_status = 'demo_only'),
    status VARCHAR(12) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'EXHAUSTED', 'EXPIRED', 'REVOKED', 'WAIVED')),
    revocation_epoch INTEGER NOT NULL DEFAULT 0,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (issuer_fid <> beneficiary_fid)
);
CREATE INDEX IF NOT EXISTS idx_federation_rr_vouchers_parties
    ON federation_rr_vouchers (beneficiary_fid, issuer_fid, status);

-- One reservation binds part of a voucher to one delegation.
CREATE TABLE IF NOT EXISTS federation_rr_reservations (
    reservation_id VARCHAR(40) PRIMARY KEY CHECK (reservation_id ~ '^frr_[a-z2-7]{26}$'),
    voucher_id VARCHAR(40) NOT NULL REFERENCES federation_rr_vouchers(voucher_id),
    federation_job_id VARCHAR(40) NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    state VARCHAR(10) NOT NULL DEFAULT 'RESERVED'
        CHECK (state IN ('RESERVED', 'CONSUMED', 'RELEASED')),
    consumed BIGINT NOT NULL DEFAULT 0 CHECK (consumed >= 0 AND consumed <= amount),
    consumed_by_source JSONB NOT NULL DEFAULT '{}'::jsonb,
    delegation_outcome VARCHAR(24),
    idempotency_key TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ,
    UNIQUE (voucher_id, idempotency_key),
    -- one reservation per delegation: a job cannot draw twice
    UNIQUE (federation_job_id)
);
CREATE INDEX IF NOT EXISTS idx_federation_rr_reservations_open
    ON federation_rr_reservations (state, created_at);

-- ── Agent Poker Arena (R2) ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS federation_arena_queue (
    entry_id VARCHAR(40) PRIMARY KEY CHECK (entry_id ~ '^faq_[a-z2-7]{26}$'),
    fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    spec_id VARCHAR(40) NOT NULL REFERENCES federation_rr_specs(spec_id),
    -- Only workspaces staking the same model class are matched (owner 2026-11-12).
    model_class VARCHAR(64) NOT NULL,
    -- sng: winner takes the stake; cash: chips are tokens, settled by final stacks.
    mode VARCHAR(8) NOT NULL DEFAULT 'sng' CHECK (mode IN ('sng', 'cash')),
    stake BIGINT NOT NULL CHECK (stake > 0),
    rules_hash VARCHAR(64) NOT NULL,
    state VARCHAR(8) NOT NULL DEFAULT 'WAITING' CHECK (state IN ('WAITING', 'MATCHED', 'LEFT')),
    match_id VARCHAR(40),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- one place in the queue per workspace
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_arena_queue_waiting
    ON federation_arena_queue (fid) WHERE state = 'WAITING';

CREATE TABLE IF NOT EXISTS federation_arena_matches (
    match_id VARCHAR(40) PRIMARY KEY CHECK (match_id ~ '^fam_[a-z2-7]{26}$'),
    seat0_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    seat1_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    seat0_spec VARCHAR(40) NOT NULL REFERENCES federation_rr_specs(spec_id),
    seat1_spec VARCHAR(40) NOT NULL REFERENCES federation_rr_specs(spec_id),
    model_class VARCHAR(64) NOT NULL,
    mode VARCHAR(8) NOT NULL DEFAULT 'sng' CHECK (mode IN ('sng', 'cash')),
    stake BIGINT NOT NULL CHECK (stake > 0),
    unit VARCHAR(8) NOT NULL DEFAULT 'DEMO_M' CHECK (unit = 'DEMO_M'),
    rules JSONB NOT NULL,
    rules_hash VARCHAR(64) NOT NULL,
    contract_hash VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL CHECK (state IN (
        'MATCHED', 'OWNER_AUTHORIZED', 'DEMO_RESERVED', 'COMMITTED', 'PLAYING',
        'RESULT_VERIFIED', 'DEMO_SETTLED', 'CLOSED',
        'ABORTED', 'TIMED_OUT', 'DISPUTED', 'RECONCILING')),
    version INTEGER NOT NULL DEFAULT 1,
    seats JSONB NOT NULL DEFAULT '[{}, {}]'::jsonb,
    -- Sealed by the host until the result: never shown before RESULT_VERIFIED.
    server_seed VARCHAR(64) NOT NULL,
    server_commit VARCHAR(64) NOT NULL,
    game JSONB,
    action_seq INTEGER NOT NULL DEFAULT 0,
    turn_deadline TIMESTAMPTZ,
    setup_deadline TIMESTAMPTZ NOT NULL,
    result JSONB,
    result_proof_hash VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (seat0_fid <> seat1_fid)
);
CREATE INDEX IF NOT EXISTS idx_federation_arena_matches_state
    ON federation_arena_matches (state, updated_at);
CREATE INDEX IF NOT EXISTS idx_federation_arena_matches_seats
    ON federation_arena_matches (seat0_fid, seat1_fid);

-- The complete action log: what replay and verification read.
CREATE TABLE IF NOT EXISTS federation_arena_actions (
    match_id VARCHAR(40) NOT NULL REFERENCES federation_arena_matches(match_id),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    seat SMALLINT NOT NULL CHECK (seat IN (0, 1)),
    action JSONB NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, seq)
);

CREATE TABLE IF NOT EXISTS federation_arena_history (
    id BIGSERIAL PRIMARY KEY,
    match_id VARCHAR(40) NOT NULL REFERENCES federation_arena_matches(match_id),
    from_state VARCHAR(16),
    to_state VARCHAR(16) NOT NULL,
    actor TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR char_length(note) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_arena_history ON federation_arena_history (match_id, id);

-- One settlement per match, ever.
CREATE TABLE IF NOT EXISTS federation_arena_settlements (
    match_id VARCHAR(40) PRIMARY KEY REFERENCES federation_arena_matches(match_id),
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('award', 'return')),
    txn_id VARCHAR(40) NOT NULL REFERENCES federation_rr_txns(txn_id),
    voucher_id VARCHAR(40) REFERENCES federation_rr_vouchers(voucher_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Delivery obligations and the arena restriction (R3) ──────────────────

CREATE TABLE IF NOT EXISTS federation_arena_obligations (
    obligation_id VARCHAR(40) PRIMARY KEY CHECK (obligation_id ~ '^fob_[a-z2-7]{26}$'),
    match_id VARCHAR(40) NOT NULL UNIQUE REFERENCES federation_arena_matches(match_id),
    obligor_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    obligee_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    voucher_id VARCHAR(40) NOT NULL REFERENCES federation_rr_vouchers(voucher_id),
    amount BIGINT NOT NULL CHECK (amount > 0),
    state VARCHAR(24) NOT NULL CHECK (state IN (
        'PENDING', 'DUE', 'DELIVERING', 'FULFILLED',
        'OVERDUE_REVIEW', 'CONFIRMED_UNFULFILLED', 'RESTRICTED', 'REMEDIATED', 'RESOLVED')),
    due_at TIMESTAMPTZ NOT NULL,
    facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    obligor_statement TEXT CHECK (obligor_statement IS NULL OR char_length(obligor_statement) <= 2000),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_arena_obligations_obligor
    ON federation_arena_obligations (obligor_fid, state);

CREATE TABLE IF NOT EXISTS federation_arena_obligation_history (
    id BIGSERIAL PRIMARY KEY,
    obligation_id VARCHAR(40) NOT NULL REFERENCES federation_arena_obligations(obligation_id),
    from_state VARCHAR(24),
    to_state VARCHAR(24) NOT NULL,
    actor TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR char_length(note) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_arena_obligation_history
    ON federation_arena_obligation_history (obligation_id, id);

-- The restriction only closes the arena's new-match doors for this
-- workspace. It is lifted automatically on remediation or resolution.
CREATE TABLE IF NOT EXISTS federation_arena_restrictions (
    restriction_id VARCHAR(40) PRIMARY KEY CHECK (restriction_id ~ '^far_[a-z2-7]{26}$'),
    fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    obligation_id VARCHAR(40) NOT NULL UNIQUE
        REFERENCES federation_arena_obligations(obligation_id),
    state VARCHAR(8) NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'LIFTED')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifted_at TIMESTAMPTZ,
    lifted_reason TEXT,
    CHECK ((state = 'LIFTED') = (lifted_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_federation_arena_restrictions_active
    ON federation_arena_restrictions (fid) WHERE state = 'ACTIVE';

REVOKE ALL ON federation_rr_specs FROM agentgraph_app;
REVOKE ALL ON federation_rr_txns FROM agentgraph_app;
REVOKE ALL ON federation_rr_entries FROM agentgraph_app;
REVOKE ALL ON federation_rr_vouchers FROM agentgraph_app;
REVOKE ALL ON federation_rr_reservations FROM agentgraph_app;
REVOKE ALL ON federation_arena_queue FROM agentgraph_app;
REVOKE ALL ON federation_arena_matches FROM agentgraph_app;
REVOKE ALL ON federation_arena_actions FROM agentgraph_app;
REVOKE ALL ON federation_arena_history FROM agentgraph_app;
REVOKE ALL ON federation_arena_settlements FROM agentgraph_app;
REVOKE ALL ON federation_arena_obligations FROM agentgraph_app;
REVOKE ALL ON federation_arena_obligation_history FROM agentgraph_app;
REVOKE ALL ON federation_arena_restrictions FROM agentgraph_app;
