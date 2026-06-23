-- migrate:up

-- Admin-managed OAuth2 MCP catalog: the set of remote MCP servers users may
-- connect to via the OAuth 2.1 broker (discover -> DCR -> PKCE -> token).
--
-- Replaces the env var MCP_CATALOG_OAUTH2 (a base64-JSON blob) as the source of
-- truth, so operators manage these servers from the admin UI instead of editing
-- an opaque env value and redeploying. On first startup the env value (if set)
-- is seeded into this table once, then the table is authoritative
-- (see app.services.mcp_oauth_catalog).
--
-- Scope: this moves ONLY the OAuth2 catalog. The static MCP_CATALOG (stdio/header
-- servers whose definitions embed creds verbatim) stays on env for now — its
-- at-rest secret handling is a separate, larger decision.
--
-- Secret posture (deliberate "for now", documented in mcp-oauth2-proxy.md):
-- client_secret is stored in-column and is NEVER serialized to the browser (the
-- admin API and the per-conversation keys endpoint expose key + non-secret
-- metadata only). Most OAuth2 MCP servers are public clients (Dynamic Client
-- Registration, no secret) so the column is usually NULL. Hardening (encrypt /
-- vault-ref, mirroring mcp_oauth_clients.client_secret_ref) is a follow-up.
CREATE TABLE inkwest.mcp_oauth_catalog (
    -- Stable catalog key (the value users select; also the mcpServers[<key>] key).
    key VARCHAR(128) PRIMARY KEY,
    -- Label override for the UI; NULL = fall back to the key.
    display_name VARCHAR(255),
    -- Streamable-HTTP MCP endpoint. Must be https (enforced in the service layer).
    url VARCHAR(1024) NOT NULL,
    -- Requested OAuth scopes; [] = let the authorization server grant its default set.
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Pre-registered client id; NULL => Dynamic Client Registration (RFC 7591) at connect.
    client_id VARCHAR(512),
    -- Pre-registered client secret; NULL for public clients. Never exposed to the browser.
    client_secret TEXT,
    -- Disabled rows stay configured but are not selectable (admin toggle; no delete needed).
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mcp_oauth_catalog_enabled
    ON inkwest.mcp_oauth_catalog (enabled) WHERE enabled;

-- migrate:down

DROP TABLE inkwest.mcp_oauth_catalog;
