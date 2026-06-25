-- migrate:up

-- Multi-user conversation sharing. A session keeps its single owner
-- (sessions.user_id); additional participants are invited here. A session is
-- "shared" iff it has at least one accepted row — no flag column on sessions.
--   role:   viewer (read-only, WS send rejected) | editor (can send messages)
--   status: pending (invited, awaiting response) | accepted | declined
-- The pending row drives the invitee's accept/decline popup; an accepted row
-- grants access (read for viewer, send for editor) checked in get_session_role.
CREATE TABLE inkwest.session_shares (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES inkwest.sessions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    -- Inviter kept for provenance; SET NULL (not CASCADE) so deleting the inviter
    -- never silently revokes a participant's standing access.
    invited_by_user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'editor' CHECK (role IN ('viewer', 'editor')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'declined')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responded_at TIMESTAMPTZ,
    -- One standing invite/membership per (session, user); re-sharing updates the row.
    UNIQUE (session_id, user_id)
);

CREATE INDEX idx_session_shares_session ON inkwest.session_shares(session_id);
-- Per-user lookups: "sessions shared with me" (sidebar union) + pending-invite recovery.
CREATE INDEX idx_session_shares_user ON inkwest.session_shares(user_id);

-- Authorship of user-sent messages in a shared conversation. NULL = the AI, or a
-- message predating sharing. SET NULL on user delete so history survives.
ALTER TABLE inkwest.messages
    ADD COLUMN author_user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL;

-- migrate:down

ALTER TABLE inkwest.messages DROP COLUMN IF EXISTS author_user_id;
DROP TABLE IF EXISTS inkwest.session_shares;
