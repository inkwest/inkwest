-- migrate:up

-- messages is the app's hottest table: the ALTER below is metadata-only (nullable, no
-- rewrite) but still takes a brief ACCESS EXCLUSIVE lock. Fail fast if something
-- long-running holds the table rather than queueing behind it and blocking every message
-- read/write. Transaction-scoped (the runner wraps each migration).
SET LOCAL lock_timeout = '5s';

-- Context gauge (spec 006): prompt size of the turn's LAST model call, the real context
-- occupancy. The per-turn token columns sum every call of a multi-iteration turn.
-- Nullable: rows predating the column, and turns from an agent image that does not
-- report it, have no data.

ALTER TABLE inkwest.messages
    ADD COLUMN context_tokens INTEGER NULL;

-- migrate:down

SET LOCAL lock_timeout = '5s';

ALTER TABLE inkwest.messages
    DROP COLUMN context_tokens;
