-- migrate:up

-- reusable, user-editable prompt templates surfaced as a palette in
-- the chat. Templates are global (no per-user ownership); content is plain text
-- with literal fill-in blanks edited in the input before sending.
CREATE TABLE inkwest.prompt_templates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_prompt_templates_active ON inkwest.prompt_templates(is_active) WHERE is_active = true;

-- migrate:down

DROP TABLE inkwest.prompt_templates;
