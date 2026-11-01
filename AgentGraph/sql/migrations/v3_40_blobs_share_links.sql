-- v3.40: first-class binary blobs, document attachments, cleanup grants and
-- public document share links.

CREATE TABLE IF NOT EXISTS blobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha256 CHAR(64) NOT NULL,
    file_name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size BIGINT NOT NULL,
    content BYTEA,
    uploader TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by TEXT,
    delete_reason TEXT,
    UNIQUE (workspace_id, sha256),
    CHECK (byte_size >= 0)
);

CREATE INDEX IF NOT EXISTS idx_blobs_workspace_created
    ON blobs (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_blobs_unreferenced
    ON blobs (workspace_id, created_at)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS document_blobs (
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    blob_id UUID NOT NULL REFERENCES blobs(id) ON DELETE RESTRICT,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (document_id, blob_id)
);

CREATE INDEX IF NOT EXISTS idx_document_blobs_blob
    ON document_blobs (blob_id);

CREATE TABLE IF NOT EXISTS blob_uploads (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    uploader TEXT NOT NULL,
    file_name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    total_parts INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS blob_upload_parts (
    upload_id UUID NOT NULL REFERENCES blob_uploads(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK (part_index >= 0),
    content BYTEA NOT NULL,
    byte_size BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (upload_id, part_index)
);

CREATE TABLE IF NOT EXISTS cleanup_grants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    grantee TEXT NOT NULL,
    scope_namespace TEXT,
    doc_key_prefix TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_cleanup_grants_grantee
    ON cleanup_grants (workspace_id, grantee, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS share_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES shared_documents(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    last_accessed_at TIMESTAMPTZ,
    access_count BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_share_links_document
    ON share_links (document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_share_links_active
    ON share_links (workspace_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS share_link_accesses (
    id BIGSERIAL PRIMARY KEY,
    link_id UUID NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_share_link_accesses_link
    ON share_link_accesses (link_id, accessed_at DESC);
