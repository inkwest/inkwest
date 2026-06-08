-- migrate:up

-- named SYSTEM prompts managed by admins. The system prompt is
-- loaded before any user interaction of a session. Global (no per-user
-- ownership); exactly one row is the default applied to non-admins. The default
-- row is seeded idempotently at app startup from app/services/prompts.py so the
-- previously hardcoded prompt now lives in the database.
CREATE TABLE inkwest.system_prompts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_system_prompts_active ON inkwest.system_prompts(is_active) WHERE is_active = true;

-- At most one default at any time.
CREATE UNIQUE INDEX idx_system_prompts_single_default
    ON inkwest.system_prompts(is_default) WHERE is_default = true;

-- migrate:down

DROP TABLE inkwest.system_prompts;
