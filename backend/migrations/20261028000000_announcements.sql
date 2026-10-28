-- migrate:up

-- Admin-written banners shown to every signed-in user (spec 004). Bounds mirror
-- app/models/schemas.py so a hand INSERT cannot store what the API would refuse.
CREATE TABLE inkwest.announcements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message TEXT NOT NULL,
    level TEXT NOT NULL,
    -- NULL = the level's own background color.
    color VARCHAR(7),
    -- NULL = automatic (black or white by background luminance).
    text_color VARCHAR(7),
    opacity SMALLINT NOT NULL DEFAULT 100,
    dismissible BOOLEAN NOT NULL DEFAULT true,
    -- DEFAULT false: a hand INSERT that omits it stays unpublished. The API requires the field.
    enabled BOOLEAN NOT NULL DEFAULT false,
    ends_at TIMESTAMPTZ,
    created_by UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Bumped on every edit: the front keys per-browser dismissal on (id, updated_at).
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Stored already trimmed: no leading/trailing char from Python's str.isspace() set, spelled
    -- out because PG's [[:space:]] depends on the locale. Interior newlines stay allowed.
    CONSTRAINT announcements_message_check CHECK (
        length(message) BETWEEN 1 AND 500
        AND message !~ '^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]|[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]$'
    ),
    CONSTRAINT announcements_level_check CHECK (level IN ('info', 'success', 'warning', 'danger')),
    CONSTRAINT announcements_color_check CHECK (color IS NULL OR color ~ '^#[0-9a-fA-F]{6}$'),
    CONSTRAINT announcements_text_color_check
        CHECK (text_color IS NULL OR text_color ~ '^#[0-9a-fA-F]{6}$'),
    CONSTRAINT announcements_opacity_check CHECK (opacity BETWEEN 10 AND 100)
);

-- The read route, hit on every page load and broadcast, only touches enabled rows.
CREATE INDEX idx_announcements_active ON inkwest.announcements (created_at) WHERE enabled;

-- migrate:down

DROP TABLE IF EXISTS inkwest.announcements;
