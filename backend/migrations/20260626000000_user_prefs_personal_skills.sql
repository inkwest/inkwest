-- migrate:up

-- Per-user preferences + personal skills.
-- Hybrid storage: a light JSONB preferences document (UI settings + the set of
-- admin skills the user turned OFF) plus a dedicated table for heavy personal
-- SKILL.md bodies — so a UI toggle never rewrites a fat blob, and the skills
-- rows reuse the admin `skills` shape (name/content/description/is_active) and
-- its skill_md.py parsing verbatim.

-- Heavy personal SKILL.md content, one row per personal skill.
CREATE TABLE inkwest.user_skills (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    -- Normalized slug (skill_md.parse_skill_md), unique within the user's set.
    name VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,           -- full SKILL.md verbatim
    description TEXT,                -- parsed from frontmatter
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    -- true => this personal skill shadows an admin skill of the same name (badge).
    is_override BOOLEAN NOT NULL DEFAULT FALSE,
    -- Provenance when forked from an admin skill. SET NULL (not CASCADE) so deleting
    -- the source admin skill never deletes the user's fork — it just loses the link.
    source_admin_skill_id UUID REFERENCES inkwest.skills(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- "Personal always wins" relies on a single materialized SKILL.md per slug per user.
    UNIQUE (user_id, name)
);

-- Per-turn lookup: "this user's active personal skills".
CREATE INDEX idx_user_skills_user ON inkwest.user_skills(user_id);

-- Light preferences document, one row per user. Stores UI settings (migrated off
-- localStorage) + the DISABLED admin-skill set (storing disabled, not enabled,
-- keeps newly-added admin skills on by default).
CREATE TABLE inkwest.user_preferences (
    user_id UUID PRIMARY KEY REFERENCES inkwest.users(id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL DEFAULT 1,
    document JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- migrate:down

DROP TABLE IF EXISTS inkwest.user_preferences;
DROP TABLE IF EXISTS inkwest.user_skills;
