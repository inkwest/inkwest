-- migrate:up

-- Per-session agentic engine (claude-cli | inkwest-agent). Reserved first-class
-- now; NULL = resolve per turn from AGENTIC_ENGINES + the model. A per-session
-- engine picker is a next step (see docs/SANDBOXED_AGENT_RUNTIME.md, decision #7).
ALTER TABLE inkwest.sessions ADD COLUMN engine VARCHAR(50);

-- migrate:down

ALTER TABLE inkwest.sessions DROP COLUMN engine;
