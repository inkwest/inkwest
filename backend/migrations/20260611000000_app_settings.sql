-- migrate:up

-- Runtime-tunable application settings managed from the admin console.
-- Simple key/value store; first key is "anthropic_model" — the default model
-- chosen by admins (absent = newest model of the provider catalog).
CREATE TABLE inkwest.app_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- migrate:down

DROP TABLE inkwest.app_settings;
