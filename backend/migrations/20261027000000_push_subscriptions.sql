-- migrate:up

-- Web Push subscriptions: one row per browser push endpoint. The endpoint is UNIQUE, not
-- (user, endpoint): one browser profile delivers to one Inkwest user at a time, so a
-- re-registration by another user moves the row to them.
CREATE TABLE inkwest.push_subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    -- Capability URL issued by the browser's push service: never log it.
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fan-out lookup: every subscription of an invitee.
CREATE INDEX idx_push_subscriptions_user ON inkwest.push_subscriptions(user_id);

-- migrate:down

DROP TABLE IF EXISTS inkwest.push_subscriptions;
