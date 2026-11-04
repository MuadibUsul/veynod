-- v3.82 Federation core tables (federation diagram chunks F-01a, F-01b, F-01c).
--
-- Contracts: doc://federation-i-id-identity-icd (ADR-001a: self-certifying ids),
-- docs/federation/ICD_I_GRANT.md, docs/federation/ICD_I_MSG.md.
--
-- Two kinds of tables (ADR-011):
--   * global, platform-only (no application-role access): identities, keys,
--     identity log, local map, addresses and address history;
--   * tenant tables (RLS, v3.48 template): peers, grants, grant history,
--     threads, messages, delivery attempts and the replay guard.
-- Nothing here is used while FEDERATION_ENABLED is false (the default), and no
-- identity is created for an existing workspace by this migration (ID-INV-5).
-- Delegation, cost, block, report and security-event tables (F-01d) follow
-- the task-bridge contract (I-BRIDGE) in a later migration.

-- ── Global, platform-only ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS federation_identities (
    federation_id VARCHAR(64) PRIMARY KEY
        CHECK (federation_id ~ '^fed:(subject|workspace|agent):[a-z2-7]{26}$'),
    id_type VARCHAR(16) NOT NULL CHECK (id_type IN ('subject', 'workspace', 'agent')),
    parent_id VARCHAR(64) REFERENCES federation_identities(federation_id),
    genesis_public_key TEXT NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'frozen', 'recovering', 'suspended', 'tombstoned')),
    key_set_version INTEGER NOT NULL DEFAULT 1 CHECK (key_set_version >= 1),
    record_hash VARCHAR(64),
    display_alias TEXT,
    hosted BOOLEAN NOT NULL DEFAULT TRUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status_changed_at TIMESTAMPTZ,
    -- ID-INV-1: the prefix names the layer.
    CHECK (federation_id LIKE 'fed:' || id_type || ':%'),
    -- ID-INV-2: subject has no parent; workspace and agent have exactly one.
    CHECK ((id_type = 'subject') = (parent_id IS NULL)),
    CHECK (expires_at > issued_at)
);
CREATE INDEX IF NOT EXISTS idx_federation_identities_parent
    ON federation_identities (parent_id);

-- Parent layer is fixed (workspace → subject, agent → workspace) and, in P1,
-- never changes (TBD-4). Identities are never deleted: tombstone instead
-- (ID-INV-6), and a tombstone is final.
CREATE OR REPLACE FUNCTION federation_identity_guard() RETURNS trigger AS $$
DECLARE
    parent_type TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'federation identities are never deleted (tombstone instead)'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.federation_id <> OLD.federation_id OR NEW.id_type <> OLD.id_type
           OR NEW.genesis_public_key <> OLD.genesis_public_key
           OR NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
            RAISE EXCEPTION 'federation identity id, type, genesis key and parent are immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.status = 'tombstoned' AND NEW.status <> 'tombstoned' THEN
            RAISE EXCEPTION 'a tombstoned federation identity cannot be revived'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.key_set_version < OLD.key_set_version THEN
            RAISE EXCEPTION 'key_set_version only increases'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.parent_id IS NOT NULL THEN
        SELECT id_type INTO parent_type FROM federation_identities
         WHERE federation_id = NEW.parent_id;
        IF (NEW.id_type = 'workspace' AND parent_type IS DISTINCT FROM 'subject')
           OR (NEW.id_type = 'agent' AND parent_type IS DISTINCT FROM 'workspace') THEN
            RAISE EXCEPTION 'federation identity % has the wrong parent layer', NEW.federation_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_federation_identity_guard ON federation_identities;
CREATE TRIGGER trg_federation_identity_guard
    BEFORE INSERT OR UPDATE OR DELETE ON federation_identities
    FOR EACH ROW EXECUTE FUNCTION federation_identity_guard();

CREATE TABLE IF NOT EXISTS federation_identity_keys (
    key_id VARCHAR(80) PRIMARY KEY CHECK (key_id ~ '^sha256:[0-9a-f]{64}$'),
    federation_id VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    alg VARCHAR(16) NOT NULL DEFAULT 'Ed25519' CHECK (alg = 'Ed25519'),
    public_key TEXT NOT NULL UNIQUE,
    -- Hosted identities only: the private key sealed with FEDERATION_KEK
    -- (ADR-013). Never the plaintext; never returned by any API.
    private_key_sealed TEXT,
    kek_id VARCHAR(64),
    state VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'retiring', 'revoked', 'expired')),
    is_genesis BOOLEAN NOT NULL DEFAULT FALSE,
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    not_after TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (state <> 'revoked' OR revoked_at IS NOT NULL),
    CHECK ((private_key_sealed IS NULL) = (kek_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_federation_identity_keys_identity
    ON federation_identity_keys (federation_id, created_at DESC);

-- Append-only log of signed identity objects (record, rotation, revocation,
-- freeze): the hash chain a verifier replays (I-ID §5).
CREATE TABLE IF NOT EXISTS federation_identity_log (
    federation_id VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    serial INTEGER NOT NULL CHECK (serial >= 1),
    object_type VARCHAR(32) NOT NULL CHECK (object_type IN (
        'identity.record', 'identity.rotation', 'identity.revocation',
        'identity.freeze', 'identity.unfreeze', 'identity.tombstone')),
    object_hash VARCHAR(64) NOT NULL,
    previous_hash VARCHAR(64),
    envelope JSONB,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (federation_id, serial),
    CHECK ((serial = 1) = (previous_hash IS NULL))
);

-- Platform-only mapping from a federation identity to local rows. These ids
-- never leave the platform (ID-INV-4, ID-INV-8).
CREATE TABLE IF NOT EXISTS federation_local_map (
    federation_id VARCHAR(64) PRIMARY KEY REFERENCES federation_identities(federation_id),
    id_type VARCHAR(16) NOT NULL CHECK (id_type IN ('subject', 'workspace', 'agent')),
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id),
    workspace_id UUID REFERENCES workspaces(id),
    principal_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((id_type = 'subject') = (workspace_id IS NULL)),
    CHECK ((id_type = 'agent') = (principal_id IS NOT NULL))
);
-- One subject per root tenant (TBD-2 placeholder), one identity per workspace,
-- one per agent principal inside a workspace.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_local_subject
    ON federation_local_map (tenant_id) WHERE id_type = 'subject';
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_local_workspace
    ON federation_local_map (workspace_id) WHERE id_type = 'workspace';
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_local_agent
    ON federation_local_map (workspace_id, principal_id) WHERE id_type = 'agent';

-- Aliases (I-ID §4). One row per (level, scope, handle) for ever: a renamed
-- handle stays as a redirect, a deleted one as a tombstone, and P1 never
-- reallocates a handle (TBD-6 placeholder), so the unique key covers all rows.
CREATE TABLE IF NOT EXISTS federation_addresses (
    address_id BIGSERIAL PRIMARY KEY,
    level VARCHAR(16) NOT NULL CHECK (level IN ('subject', 'workspace', 'agent')),
    scope_id VARCHAR(64) NOT NULL DEFAULT '',
    handle TEXT NOT NULL CHECK (char_length(handle) BETWEEN 1 AND 64),
    display_handle TEXT NOT NULL,
    target_id VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'redirect', 'tombstoned')),
    redirect_to BIGINT REFERENCES federation_addresses(address_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((status = 'redirect') = (redirect_to IS NOT NULL)),
    CHECK ((level = 'subject') = (scope_id = ''))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_addresses_handle
    ON federation_addresses (level, scope_id, handle);
-- At most one active alias per identity.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_addresses_active_target
    ON federation_addresses (target_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_federation_addresses_handle
    ON federation_addresses (handle);

CREATE TABLE IF NOT EXISTS federation_address_history (
    history_id BIGSERIAL PRIMARY KEY,
    address_id BIGINT NOT NULL REFERENCES federation_addresses(address_id),
    action VARCHAR(16) NOT NULL CHECK (action IN ('create', 'rename', 'tombstone')),
    old_handle TEXT,
    new_handle TEXT,
    target_id VARCHAR(64) NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Tenant tables (RLS) ──────────────────────────────────────────────────

-- What this workspace knows about a remote workspace identity (its own copy).
CREATE TABLE IF NOT EXISTS federation_peers (
    peer_row_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    peer_workspace_fid VARCHAR(64) NOT NULL
        CHECK (peer_workspace_fid ~ '^fed:workspace:[a-z2-7]{26}$'),
    peer_subject_fid VARCHAR(64)
        CHECK (peer_subject_fid IS NULL OR peer_subject_fid ~ '^fed:subject:[a-z2-7]{26}$'),
    trust VARCHAR(16) NOT NULL DEFAULT 'unpinned' CHECK (trust IN ('pinned', 'unpinned')),
    pinned_by TEXT,
    key_set_version_seen INTEGER,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, peer_workspace_fid)
);

CREATE TABLE IF NOT EXISTS federation_grants (
    connection_id VARCHAR(40) NOT NULL CHECK (connection_id ~ '^fcn_[a-z2-7]{26}$'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    local_workspace_fid VARCHAR(64) NOT NULL
        CHECK (local_workspace_fid ~ '^fed:workspace:[a-z2-7]{26}$'),
    peer_workspace_fid VARCHAR(64) NOT NULL
        CHECK (peer_workspace_fid ~ '^fed:workspace:[a-z2-7]{26}$'),
    peer_subject_fid VARCHAR(64),
    initiator VARCHAR(8) NOT NULL CHECK (initiator IN ('local', 'peer')),
    state VARCHAR(16) NOT NULL CHECK (state IN (
        'REQUESTED', 'ACTIVE', 'REJECTED', 'EXPIRED', 'SUSPENDED', 'REVOKED', 'BLOCKED')),
    outbound_scopes TEXT[] NOT NULL DEFAULT '{}',
    inbound_scopes TEXT[] NOT NULL DEFAULT '{}',
    grant_version INTEGER NOT NULL CHECK (grant_version >= 1),
    revocation_epoch INTEGER NOT NULL DEFAULT 0 CHECK (revocation_epoch >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    peer_key_set_version INTEGER,
    encryption_policy VARCHAR(24) NOT NULL DEFAULT 'server_readable'
        CHECK (encryption_policy IN ('server_readable')),
    may_redelegate BOOLEAN NOT NULL DEFAULT FALSE CHECK (may_redelegate = FALSE),
    reason TEXT NOT NULL CHECK (char_length(reason) >= 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, connection_id),
    CHECK (local_workspace_fid <> peer_workspace_fid),
    CHECK (outbound_scopes <@ ARRAY['chat', 'delegate', 'presence']::TEXT[]),
    CHECK (inbound_scopes <@ ARRAY['chat', 'delegate', 'presence']::TEXT[])
);
-- One live connection per peer; terminal ones stay for audit.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_grants_live_peer
    ON federation_grants (workspace_id, peer_workspace_fid)
    WHERE state IN ('REQUESTED', 'ACTIVE', 'EXPIRED', 'SUSPENDED');

CREATE TABLE IF NOT EXISTS federation_grant_history (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    connection_id VARCHAR(40) NOT NULL,
    grant_version INTEGER NOT NULL CHECK (grant_version >= 1),
    action VARCHAR(24) NOT NULL CHECK (action IN (
        'request', 'approve', 'reject', 'expire', 'renew', 'freeze', 'unfreeze',
        'revoke', 'block', 'change_scopes')),
    from_state VARCHAR(16),
    to_state VARCHAR(16) NOT NULL,
    outbound_scopes TEXT[] NOT NULL,
    inbound_scopes TEXT[] NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revocation_epoch INTEGER NOT NULL,
    actor TEXT NOT NULL CHECK (actor ~ '^(user|agent|peer):.+$' OR actor = 'system'),
    approved_by TEXT CHECK (approved_by IS NULL OR approved_by LIKE 'user:%'),
    policy_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, connection_id, grant_version),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES federation_grants (workspace_id, connection_id),
    -- GRANT-INV-2: decisions that need a person name one.
    CHECK (action NOT IN ('approve', 'renew', 'unfreeze') OR approved_by IS NOT NULL)
);

-- GRANT-INV-5/6: history is append-only and versions go up by exactly one.
CREATE OR REPLACE FUNCTION federation_grant_history_guard() RETURNS trigger AS $$
DECLARE
    latest INTEGER;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'federation grant history is append-only'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT max(grant_version) INTO latest FROM federation_grant_history
     WHERE workspace_id = NEW.workspace_id AND connection_id = NEW.connection_id;
    IF NEW.grant_version <> COALESCE(latest, 0) + 1 THEN
        RAISE EXCEPTION 'grant_version must be % (got %)', COALESCE(latest, 0) + 1, NEW.grant_version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_federation_grant_history_guard ON federation_grant_history;
CREATE TRIGGER trg_federation_grant_history_guard
    BEFORE INSERT OR UPDATE OR DELETE ON federation_grant_history
    FOR EACH ROW EXECUTE FUNCTION federation_grant_history_guard();

CREATE TABLE IF NOT EXISTS federation_threads (
    thread_id VARCHAR(40) NOT NULL,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    connection_id VARCHAR(40) NOT NULL,
    peer_workspace_fid VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, thread_id),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES federation_grants (workspace_id, connection_id)
);

CREATE TABLE IF NOT EXISTS federation_messages (
    message_row_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    direction VARCHAR(3) NOT NULL CHECK (direction IN ('in', 'out')),
    message_id VARCHAR(64) NOT NULL,
    connection_id VARCHAR(40) NOT NULL,
    thread_id VARCHAR(40),
    peer_workspace_fid VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    sender_fid VARCHAR(64) NOT NULL,
    recipient_fid VARCHAR(64),
    grant_version INTEGER NOT NULL,
    -- NULL once the TTL tombstone clears the body (ICD_I_MSG §4.3).
    payload JSONB,
    payload_encoding VARCHAR(16) NOT NULL DEFAULT 'plain' CHECK (payload_encoding IN ('plain')),
    key_ref TEXT CHECK (key_ref IS NULL),
    payload_sha256 VARCHAR(64) NOT NULL,
    envelope JSONB,
    content_origin VARCHAR(16) NOT NULL DEFAULT 'federation',
    state VARCHAR(16) NOT NULL CHECK (state IN (
        'accepted_locally', 'sent', 'received', 'processed', 'rejected',
        'expired', 'dead_letter')),
    reject_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    tombstoned_at TIMESTAMPTZ,
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES federation_grants (workspace_id, connection_id),
    CHECK ((tombstoned_at IS NULL) OR (payload IS NULL))
);
-- F-007: a message id and an idempotency key each produce one business row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_messages_id
    ON federation_messages (workspace_id, direction, peer_workspace_fid, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_messages_idempotency
    ON federation_messages (workspace_id, direction, connection_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_federation_messages_thread
    ON federation_messages (workspace_id, thread_id, created_at);

CREATE TABLE IF NOT EXISTS federation_delivery_attempts (
    attempt_row_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    message_row_id BIGINT NOT NULL REFERENCES federation_messages(message_row_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    result VARCHAR(16) NOT NULL CHECK (result IN ('pending', 'ok', 'retry', 'dead_letter')),
    error_code VARCHAR(64),
    next_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (message_row_id, attempt_no),
    CHECK (result <> 'retry' OR next_retry_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_federation_delivery_due
    ON federation_delivery_attempts (next_retry_at) WHERE result = 'retry';

-- Signatures already accepted by this workspace (ICD_I_MSG §3 step 7).
CREATE TABLE IF NOT EXISTS federation_replay_guard (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    signature_digest VARCHAR(64) NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, signature_digest)
);
CREATE INDEX IF NOT EXISTS idx_federation_replay_guard_expiry
    ON federation_replay_guard (expires_at);

-- ── Privileges, tenant derivation and RLS ────────────────────────────────

REVOKE ALL ON federation_identities FROM agentgraph_app;
REVOKE ALL ON federation_identity_keys FROM agentgraph_app;
REVOKE ALL ON federation_identity_log FROM agentgraph_app;
REVOKE ALL ON federation_local_map FROM agentgraph_app;
REVOKE ALL ON federation_addresses FROM agentgraph_app;
REVOKE ALL ON federation_address_history FROM agentgraph_app;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'federation_peers', 'federation_grants', 'federation_grant_history',
        'federation_threads', 'federation_messages', 'federation_delivery_attempts',
        'federation_replay_guard'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', t);
        EXECUTE format('CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
                       'workspace_id, tenant_id ON %I FOR EACH ROW '
                       'EXECUTE FUNCTION agentgraph_derive_tenant()', t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                       'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                       'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO agentgraph_app', t);
    END LOOP;
    -- History stays insert-only for the application role as well.
    EXECUTE 'REVOKE UPDATE, DELETE ON federation_grant_history FROM agentgraph_app';
END $$;

GRANT USAGE, SELECT ON SEQUENCE federation_peers_peer_row_id_seq TO agentgraph_app;
GRANT USAGE, SELECT ON SEQUENCE federation_messages_message_row_id_seq TO agentgraph_app;
GRANT USAGE, SELECT ON SEQUENCE federation_delivery_attempts_attempt_row_id_seq TO agentgraph_app;
