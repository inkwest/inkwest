-- migrate:up

-- Per-message agentic engine: the engine that actually produced this assistant
-- turn (claude-cli | inkwest-agent, or the provider transport for non-CLI
-- deployments), captured at generation time alongside the per-turn model. The
-- session-level engine (sessions.engine) is NULL when resolved per turn and can
-- change mid-conversation, so it cannot reconstruct what an older message ran on.
-- NULL for pre-existing rows and for non-assistant messages.
ALTER TABLE inkwest.messages ADD COLUMN engine VARCHAR(50);

-- migrate:down

ALTER TABLE inkwest.messages DROP COLUMN IF EXISTS engine;
