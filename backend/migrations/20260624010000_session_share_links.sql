-- migrate:up

-- Revokable, anonymous read-only share links for a conversation. Each row is one
-- shareable URL: anyone holding the token gets view-only access WITHOUT an Inkwest
-- login (see GET /api/shared/{token} and the /ws/shared/{token} WebSocket). An owner
-- may mint several links and revoke each independently — revoking sets is_active
-- false (kept as an audit trail rather than deleted) and disconnects live guests.
CREATE TABLE inkwest.session_share_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES inkwest.sessions(id) ON DELETE CASCADE,
    -- High-entropy URL-safe secret (secrets.token_urlsafe). The bearer credential
    -- for the link, so it is unique and the lookup index.
    token VARCHAR(64) NOT NULL UNIQUE,
    created_by_user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    label VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX idx_session_share_links_session ON inkwest.session_share_links(session_id);
-- Token lookup on every anonymous request hits this; partial to the live links.
CREATE INDEX idx_session_share_links_active_token
    ON inkwest.session_share_links(token) WHERE is_active = true;

-- migrate:down

DROP TABLE IF EXISTS inkwest.session_share_links;
