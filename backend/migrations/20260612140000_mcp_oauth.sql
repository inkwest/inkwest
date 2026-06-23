-- migrate:up

-- OAuth2 MCP brokering (see docs/mcp-oauth2-proxy.md). Inkwest brokers the OAuth 2.1
-- flow for remote MCP servers listed in MCP_CATALOG_OAUTH2 and holds per-user grants.
-- Token MATERIAL never lives in these tables: it is encrypted under a per-user DEK
-- that is wrapped by a browser-held key (mcp_user_keys) and stored as ciphertext.

-- Dynamic Client Registration results + discovered endpoints, one row per catalog key.
-- token_endpoint/revocation_endpoint are persisted so the refresh hot path never has to
-- re-run metadata discovery (latency + an AS-metadata outage must not break refresh).
CREATE TABLE inkwest.mcp_oauth_clients (
    mcp_key             VARCHAR(255) PRIMARY KEY,
    issuer              TEXT NOT NULL,
    token_endpoint      TEXT NOT NULL,
    revocation_endpoint TEXT,
    client_id           TEXT NOT NULL,
    client_secret_ref   TEXT,            -- vault_ref, nullable (public client)
    client_expires_at   TIMESTAMPTZ,     -- RFC 7591 client_secret_expires_at; re-register past it
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user MCP grant. Token ciphertext is held inline (blob/iv) for this increment;
-- vault_ref is the opaque handle so a future shared vault is a backend swap, not a
-- schema change. status mirrors platform_grants: active | expired | revoked.
CREATE TABLE inkwest.mcp_user_tokens (
    user_id     UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    mcp_key     VARCHAR(255) NOT NULL,
    vault_ref   TEXT NOT NULL,
    blob        BYTEA,                   -- AES-256-GCM ciphertext of {access_token,…}
    blob_iv     BYTEA,                   -- GCM nonce for blob
    expires_at  TIMESTAMPTZ,             -- nullable: some AS issue non-expiring tokens
    scopes      TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, mcp_key)
);

-- In-flight authorization states; mirrors connector_pending_states column-for-column,
-- with mcp_key in place of the connector domain. Single-use, short-lived (TTL constant).
CREATE TABLE inkwest.mcp_oauth_pending_states (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state         VARCHAR(255) NOT NULL UNIQUE,
    code_verifier VARCHAR(128) NOT NULL,
    mcp_key       VARCHAR(255) NOT NULL,
    user_id       UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mcp_oauth_pending_created ON inkwest.mcp_oauth_pending_states(created_at);

-- Browser-fractioned key custody (audit-doc Phase C): the per-user DEK that encrypts
-- token blobs, WRAPPED by a non-extractable WebCrypto key that lives only in the user's
-- browser (IndexedDB). The server stores the wrapped form + IV and never the unwrapping
-- key, so a DB+disk+env dump with nobody connected yields no plaintext.
CREATE TABLE inkwest.mcp_user_keys (
    user_id     UUID PRIMARY KEY REFERENCES inkwest.users(id) ON DELETE CASCADE,
    wrapped_dek BYTEA NOT NULL,
    wrap_alg    VARCHAR(32) NOT NULL,
    wrap_iv     BYTEA,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MCP grant lifecycle events are user-level (no conversation): relax the audit constraint.
-- connector_audit_log already allows NULL user_id; session_id was the only NOT NULL block.
ALTER TABLE inkwest.connector_audit_log ALTER COLUMN session_id DROP NOT NULL;

-- migrate:down

-- WARNING (operator): this rollback PERMANENTLY DELETES all user-level audit rows
-- (session_id IS NULL) — i.e. the entire MCP grant/revoke audit history. It is required
-- because re-asserting NOT NULL would otherwise fail while those rows exist. Export the
-- audit log first if that history must be retained.
DELETE FROM inkwest.connector_audit_log WHERE session_id IS NULL;
ALTER TABLE inkwest.connector_audit_log ALTER COLUMN session_id SET NOT NULL;

DROP TABLE IF EXISTS inkwest.mcp_user_keys;
DROP TABLE IF EXISTS inkwest.mcp_oauth_pending_states;
DROP TABLE IF EXISTS inkwest.mcp_user_tokens;
DROP TABLE IF EXISTS inkwest.mcp_oauth_clients;
