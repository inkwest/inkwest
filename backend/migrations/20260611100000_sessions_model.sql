-- migrate:up

-- Per-session model choice: users pick a model (from the admin-enabled list)
-- when starting a chat and may change it mid-session. NULL = use the global
-- default resolved at turn time.
ALTER TABLE inkwest.sessions ADD COLUMN model VARCHAR(255);

-- migrate:down

ALTER TABLE inkwest.sessions DROP COLUMN model;
