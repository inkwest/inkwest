-- migrate:up

-- Cached tool enumeration per MCP server. The backend enumerates a server's tools
-- server-side (MCP initialize + tools/list over streamable-HTTP, creds injected by the
-- proxy layer) so the UI shows every tool up front and the user pre-sets per-tool
-- permissions before any turn runs. Refreshed on connect / on demand.
--
-- Server-global (keyed by server_key only). If a server's advertised tool set ever becomes
-- user-specific (OAuth-scope divergence), re-key by (user_id, server_key) — deferred.
CREATE TABLE inkwest.mcp_server_tools (
    -- Catalog key (oauth or static); matches mcpServers[<key>].
    server_key VARCHAR(128) PRIMARY KEY,
    -- [{name, description, input_schema}] verbatim from the server's tools/list.
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- created_at = first enumeration; updated_at = last refresh (shown as freshness).
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- migrate:down

DROP TABLE inkwest.mcp_server_tools;
