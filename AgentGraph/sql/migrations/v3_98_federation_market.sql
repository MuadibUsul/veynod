-- v3.98 Federation market: offers, contracts, escrow and a simulated ledger
-- (task P5-1, docs/federation/ICD_P5_MARKET.md).
--
-- SIMULATED ONLY. The unit is SIM, a demonstration credit: not money, not
-- bought, not withdrawn, not transferable outside this deployment. Real
-- settlement needs the medium, the escrow holder and the fee rules decided
-- (vision §19.2), and a separate review.
--
-- Offers and contracts are the shared layer over three value streams (task,
-- skill, tool). Escrow and settlement are separate: a contract's state
-- change only queues a settlement instruction; the settlement step moves
-- credits in a double-entry ledger whose every transaction sums to zero.

CREATE TABLE IF NOT EXISTS federation_offers (
    offer_id VARCHAR(40) PRIMARY KEY CHECK (offer_id ~ '^fof_[a-z2-7]{26}$'),
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('task', 'skill', 'tool')),
    seller_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    buyer_fid VARCHAR(64) REFERENCES federation_identities(federation_id),
    subject TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 200),
    price BIGINT NOT NULL CHECK (price > 0),
    unit VARCHAR(8) NOT NULL DEFAULT 'SIM' CHECK (unit = 'SIM'),
    terms_hash VARCHAR(64) NOT NULL,
    state VARCHAR(12) NOT NULL DEFAULT 'OPEN'
        CHECK (state IN ('OPEN', 'ACCEPTED', 'WITHDRAWN', 'EXPIRED')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (buyer_fid IS NULL OR buyer_fid <> seller_fid)
);
CREATE INDEX IF NOT EXISTS idx_federation_offers_seller ON federation_offers (seller_fid, state);
CREATE INDEX IF NOT EXISTS idx_federation_offers_buyer ON federation_offers (buyer_fid, state);

CREATE TABLE IF NOT EXISTS federation_contracts (
    contract_id VARCHAR(40) PRIMARY KEY CHECK (contract_id ~ '^fct_[a-z2-7]{26}$'),
    offer_id VARCHAR(40) NOT NULL UNIQUE REFERENCES federation_offers(offer_id),
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('task', 'skill', 'tool')),
    seller_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    buyer_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    subject TEXT NOT NULL,
    price BIGINT NOT NULL CHECK (price > 0),
    unit VARCHAR(8) NOT NULL DEFAULT 'SIM' CHECK (unit = 'SIM'),
    terms_hash VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_contracts_parties
    ON federation_contracts (buyer_fid, seller_fid, state);

CREATE TABLE IF NOT EXISTS federation_contract_history (
    id BIGSERIAL PRIMARY KEY,
    contract_id VARCHAR(40) NOT NULL REFERENCES federation_contracts(contract_id),
    from_state VARCHAR(16),
    to_state VARCHAR(16) NOT NULL,
    actor TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR char_length(note) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_contract_history
    ON federation_contract_history (contract_id, id);

-- Double-entry ledger. Accounts: 'ws:<fid>' (a workspace's credits),
-- 'escrow:<contract_id>', 'mint' (where demonstration credits come from).
CREATE TABLE IF NOT EXISTS federation_ledger_txns (
    txn_id VARCHAR(40) PRIMARY KEY CHECK (txn_id ~ '^ftx_[a-z2-7]{26}$'),
    kind VARCHAR(16) NOT NULL
        CHECK (kind IN ('allowance', 'mint', 'escrow_hold', 'release', 'refund')),
    reference TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS federation_ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    txn_id VARCHAR(40) NOT NULL REFERENCES federation_ledger_txns(txn_id),
    account TEXT NOT NULL CHECK (account ~ '^(ws:fed:workspace:[a-z2-7]{26}|escrow:fct_[a-z2-7]{26}|mint)$'),
    amount BIGINT NOT NULL CHECK (amount <> 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_ledger_account ON federation_ledger_entries (account, id);

-- Every transaction balances, checked when it commits.
CREATE OR REPLACE FUNCTION federation_ledger_balanced() RETURNS trigger AS $$
DECLARE
    total BIGINT;
BEGIN
    SELECT COALESCE(sum(amount), 0) INTO total FROM federation_ledger_entries
     WHERE txn_id = NEW.txn_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'ledger transaction % does not balance (%)', NEW.txn_id, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_ledger_balanced ON federation_ledger_entries;
CREATE CONSTRAINT TRIGGER trg_federation_ledger_balanced
    AFTER INSERT ON federation_ledger_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION federation_ledger_balanced();

-- The ledger is append-only.
CREATE OR REPLACE FUNCTION federation_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the ledger is append-only' USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_ledger_entries_guard ON federation_ledger_entries;
CREATE TRIGGER trg_federation_ledger_entries_guard BEFORE UPDATE OR DELETE
    ON federation_ledger_entries FOR EACH ROW EXECUTE FUNCTION federation_ledger_append_only();
DROP TRIGGER IF EXISTS trg_federation_ledger_txns_guard ON federation_ledger_txns;
CREATE TRIGGER trg_federation_ledger_txns_guard BEFORE UPDATE OR DELETE
    ON federation_ledger_txns FOR EACH ROW EXECUTE FUNCTION federation_ledger_append_only();

-- Settlement instructions: what a contract's state change asks the
-- settlement step to do. Executed once each (state and txn_id).
CREATE TABLE IF NOT EXISTS federation_settlements (
    instruction_id VARCHAR(40) PRIMARY KEY CHECK (instruction_id ~ '^fst_[a-z2-7]{26}$'),
    contract_id VARCHAR(40) NOT NULL REFERENCES federation_contracts(contract_id),
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('release', 'refund')),
    amount BIGINT NOT NULL CHECK (amount > 0),
    state VARCHAR(8) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
    txn_id VARCHAR(40) REFERENCES federation_ledger_txns(txn_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ,
    CHECK ((state = 'done') = (txn_id IS NOT NULL))
);
-- One release or refund per contract, ever.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_settlements_contract
    ON federation_settlements (contract_id);

REVOKE ALL ON federation_offers FROM agentgraph_app;
REVOKE ALL ON federation_contracts FROM agentgraph_app;
REVOKE ALL ON federation_contract_history FROM agentgraph_app;
REVOKE ALL ON federation_ledger_txns FROM agentgraph_app;
REVOKE ALL ON federation_ledger_entries FROM agentgraph_app;
REVOKE ALL ON federation_settlements FROM agentgraph_app;
