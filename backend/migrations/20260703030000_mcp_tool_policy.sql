-- migrate:up

-- Per-tool MCP permission policy, two scopes:
--   * user_mcp_tool_policy    — a user's global defaults (persist across conversations)
--   * session_mcp_tool_policy — per-conversation overrides
--
-- A row with tool = '*' is the SERVER DEFAULT for that scope (tools inherit it unless a
-- specific tool row overrides). verdict is one of allow | deny | ask.
--
-- Resolution (most specific wins), see app.services.mcp_policy:
--   conv/tool -> conv/server(*) -> global/tool -> global/server(*) -> system default (ask)
--
-- Whole-server enablement stays in sessions.enabled_mcp_servers: a server that is not
-- enabled contributes no tools regardless of policy.

CREATE TABLE inkwest.user_mcp_tool_policy (
    user_id UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    -- Catalog key (oauth or static); matches mcpServers[<key>].
    server_key VARCHAR(128) NOT NULL,
    -- Tool name, or '*' for the server-wide default.
    tool VARCHAR(255) NOT NULL,
    verdict VARCHAR(8) NOT NULL CHECK (verdict IN ('allow', 'deny', 'ask')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, server_key, tool)
);

CREATE TABLE inkwest.session_mcp_tool_policy (
    session_id UUID NOT NULL REFERENCES inkwest.sessions(id) ON DELETE CASCADE,
    server_key VARCHAR(128) NOT NULL,
    tool VARCHAR(255) NOT NULL,
    verdict VARCHAR(8) NOT NULL CHECK (verdict IN ('allow', 'deny', 'ask')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, server_key, tool)
);

-- migrate:down

DROP TABLE inkwest.session_mcp_tool_policy;
DROP TABLE inkwest.user_mcp_tool_policy;
