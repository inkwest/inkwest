-- migrate:up

-- Manual, stable ordering for the session sidebar. Sessions were listed by
-- updated_at DESC, so any activity bumped a conversation to the top. `position`
-- gives each session an explicit slot (ASC = top) that only an explicit reorder
-- changes; new sessions get the smallest value so they appear on top.
ALTER TABLE inkwest.sessions ADD COLUMN position INTEGER NOT NULL DEFAULT 0;

-- Seed a deterministic order from creation time (newest first) per user, so the
-- list starts in a stable arrangement instead of every row tying at 0.
WITH ordered AS (
    SELECT id, (ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) - 1) AS pos
    FROM inkwest.sessions
)
UPDATE inkwest.sessions s
SET position = ordered.pos
FROM ordered
WHERE s.id = ordered.id;

CREATE INDEX idx_sessions_user_position ON inkwest.sessions(user_id, position);

-- migrate:down

DROP INDEX IF EXISTS inkwest.idx_sessions_user_position;
ALTER TABLE inkwest.sessions DROP COLUMN IF EXISTS position;
