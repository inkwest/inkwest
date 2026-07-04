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

-- Deliberately NOT CONCURRENTLY: the runner wraps migrations in a transaction (where
-- CONCURRENTLY is illegal), and the build's SHARE lock blocks writes only for the scan of
-- a today-small table on a single-replica deploy. lock_timeout above bounds acquisition,
-- not the build — if usage_records grows large, move these to a manual
-- CREATE INDEX CONCURRENTLY outside the migration runner.
CREATE INDEX ix_usage_records_turn_id ON inkwest.usage_records (turn_id);

-- One engine-side budget row per turn, enforced at the DB. The orchestrators guarantee a
-- single persist per turn (usage_persisted flag + fresh turn_id per execution); this
-- closes the residual ambiguous-commit race (commit succeeded server-side, ack lost,
-- retry's existence probe raced a healing connection): a re-applied INSERT now fails and
-- rolls the whole retry transaction back — no doubled spend, no doubled session stats.
-- Broker rows (N per turn) and pre-cutover NULL turn_ids are excluded.
CREATE UNIQUE INDEX uq_usage_records_engine_turn
    ON inkwest.usage_records (turn_id)
    WHERE source = 'engine' AND turn_id IS NOT NULL;

-- migrate:down

SET LOCAL lock_timeout = '5s';

DROP INDEX inkwest.uq_usage_records_engine_turn;
DROP INDEX inkwest.ix_usage_records_turn_id;

ALTER TABLE inkwest.usage_records
    DROP COLUMN cache_read_tokens,
    DROP COLUMN cache_write_tokens,
    DROP COLUMN source,
    DROP COLUMN turn_id;
