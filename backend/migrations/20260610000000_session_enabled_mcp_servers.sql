-- migrate:up

-- Per-conversation MCP selection: the list of catalog keys (see MCP_CATALOG /
-- app.services.mcp_catalog) the user enabled for this session. Persisted so the
-- choice survives reload / --resume. JSONB (not a text[]) leaves room to grow
-- into per-server options later without another migration. Defaults to [] so
-- existing sessions start with no MCP enabled.
ALTER TABLE inkwest.sessions
    ADD COLUMN enabled_mcp_servers JSONB NOT NULL DEFAULT '[]'::jsonb;

-- migrate:down

ALTER TABLE inkwest.sessions DROP COLUMN IF EXISTS enabled_mcp_servers;
