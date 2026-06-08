-- migrate:up

-- persist the system prompt chosen at session creation. Both columns
-- null → the session resolves the global default. system_prompt_custom (admin
-- free-text) takes precedence over system_prompt_id when both are set.
ALTER TABLE inkwest.sessions
    ADD COLUMN system_prompt_id UUID REFERENCES inkwest.system_prompts(id) ON DELETE SET NULL,
    ADD COLUMN system_prompt_custom TEXT;

CREATE INDEX idx_sessions_system_prompt_id ON inkwest.sessions(system_prompt_id);

-- migrate:down

ALTER TABLE inkwest.sessions
    DROP COLUMN system_prompt_id,
    DROP COLUMN system_prompt_custom;
