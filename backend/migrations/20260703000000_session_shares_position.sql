-- migrate:up

-- Per-user manual ordering of shared-in conversations. sessions.position is the
-- OWNER's arrangement; a participant (shared-with-me) had nowhere to store their
-- own slot, so dragging such a row in the sidebar silently reverted to the owner's
-- order. This column gives each accepted participant their own position for the
-- session (ASC = top), mirroring sessions.position. Owned rows keep using
-- sessions.position; the sidebar's manual sort coalesces the two per caller.
ALTER TABLE inkwest.session_shares ADD COLUMN position INTEGER NOT NULL DEFAULT 0;

-- Seed a deterministic per-user order from share creation time (newest first), so
-- shared-in lists start stably instead of every row tying at 0.
WITH ordered AS (
    SELECT id, (ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) - 1) AS pos
    FROM inkwest.session_shares
)
UPDATE inkwest.session_shares s
SET position = ordered.pos
FROM ordered
WHERE s.id = ordered.id;

-- Per-user lookup for the sidebar's manual-order join.
CREATE INDEX idx_session_shares_user_position ON inkwest.session_shares(user_id, position);

-- migrate:down

DROP INDEX IF EXISTS inkwest.idx_session_shares_user_position;
ALTER TABLE inkwest.session_shares DROP COLUMN IF EXISTS position;
