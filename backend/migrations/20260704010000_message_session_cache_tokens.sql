-- migrate:up

-- messages is the app's hottest table: the ALTERs below are metadata-only (nullable /
-- constant default, no rewrite) but still take a brief ACCESS EXCLUSIVE lock. Fail fast
-- if something long-running holds the table rather than queueing behind it and blocking
-- every message read/write. Transaction-scoped (the runner wraps each migration).
SET LOCAL lock_timeout = '5s';

-- Per-bubble + per-session prompt-cache surfacing (budget accuracy):
--   * messages: per-turn cache splits next to the existing input/output counts
--     (nullable — rows predating this column simply have no data).
--   * sessions: cumulative cache counters feeding the header/list token badges
--     (counter semantics, NOT NULL DEFAULT 0 like the existing totals).

ALTER TABLE inkwest.messages
    ADD COLUMN cache_read_tokens INTEGER NULL,
    ADD COLUMN cache_write_tokens INTEGER NULL;

ALTER TABLE inkwest.sessions
    ADD COLUMN total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN total_cache_write_tokens INTEGER NOT NULL DEFAULT 0;

-- migrate:down

SET LOCAL lock_timeout = '5s';

ALTER TABLE inkwest.messages
    DROP COLUMN cache_read_tokens,
    DROP COLUMN cache_write_tokens;

ALTER TABLE inkwest.sessions
    DROP COLUMN total_cache_read_tokens,
    DROP COLUMN total_cache_write_tokens;
