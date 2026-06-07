-- migrate:up

-- Pending connector OAuth handshakes (state + PKCE verifier), consumed once.
CREATE TABLE inkwest.connector_pending_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state VARCHAR(64) NOT NULL UNIQUE,
    code_verifier VARCHAR(128) NOT NULL,
    connector VARCHAR(32) NOT NULL,
    user_id UUID NOT NULL,
    session_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_connector_pending_states_created
    ON inkwest.connector_pending_states(created_at);

-- Audit trail of connector access grants. Never contains secrets.
CREATE TABLE inkwest.connector_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID,
    session_id UUID NOT NULL,
    connector VARCHAR(32) NOT NULL,
    action VARCHAR(32) NOT NULL,
    scope VARCHAR(255),
    selection JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_connector_audit_user ON inkwest.connector_audit_log(user_id);
CREATE INDEX idx_connector_audit_session ON inkwest.connector_audit_log(session_id);
CREATE INDEX idx_connector_audit_occurred ON inkwest.connector_audit_log(occurred_at DESC);

-- migrate:down

DROP TABLE IF EXISTS inkwest.connector_audit_log;
DROP TABLE IF EXISTS inkwest.connector_pending_states;
