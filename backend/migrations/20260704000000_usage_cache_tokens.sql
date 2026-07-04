-- migrate:up

-- usage_records is written on every turn (and, post-branch, every Bedrock call) and
-- scanned by the usage dashboards: the ALTER takes ACCESS EXCLUSIVE and would queue
-- behind any long report scan, pinning every insert behind it. Fail fast instead.
-- Transaction-scoped (the runner wraps each migration).
SET LOCAL lock_timeout = '5s';

-- Prompt-cache-aware usage accounting (budget accuracy):
--   * cache_read_tokens / cache_write_tokens — cache tokens were previously dropped
--     entirely (~10x cost undercount on cache-heavy Claude Code turns).
--   * source — which authority wrote the row: 'engine' (orchestrator accounting) or
--     'broker' (LLM proxy metering, one row per Bedrock call).
--   * turn_id — per-turn aggregation key shared by the N broker rows of one turn.
--     Nullable, no default: pre-cutover rows stay NULL and simply don't group.

ALTER TABLE inkwest.usage_records
    ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'engine',
    ADD COLUMN turn_id UUID NULL;

CREATE INDEX ix_usage_records_turn_id ON inkwest.usage_records (turn_id);

-- migrate:down

SET LOCAL lock_timeout = '5s';

DROP INDEX inkwest.ix_usage_records_turn_id;

ALTER TABLE inkwest.usage_records
    DROP COLUMN cache_read_tokens,
    DROP COLUMN cache_write_tokens,
    DROP COLUMN source,
    DROP COLUMN turn_id;
