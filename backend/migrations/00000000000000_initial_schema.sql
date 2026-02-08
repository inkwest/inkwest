-- migrate:up

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TYPE budgettype AS ENUM ('global', 'user', 'role');
CREATE TYPE budgetperiod AS ENUM ('hourly', 'daily', 'weekly', 'monthly');

CREATE TABLE inkwest.clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    kubeconfig_path VARCHAR(1024) NOT NULL,
    context_name VARCHAR(255),
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_clusters_active ON inkwest.clusters(is_active) WHERE is_active = true;

CREATE TABLE inkwest.users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    oidc_subject VARCHAR(255) NOT NULL UNIQUE,
    oidc_issuer VARCHAR(512) NOT NULL DEFAULT 'local',
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    roles TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_users_oidc_subject ON inkwest.users(oidc_subject);
CREATE INDEX idx_users_email ON inkwest.users(email);

CREATE TABLE inkwest.sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    cluster_id UUID NOT NULL REFERENCES inkwest.clusters(id) ON DELETE CASCADE,
    title VARCHAR(255),
    summary TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT false,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tool_calls INTEGER NOT NULL DEFAULT 0,
    total_iterations INTEGER NOT NULL DEFAULT 0,
    confirmation_mode VARCHAR(50) NOT NULL DEFAULT 'expensive',
    cost_confirmation_threshold INTEGER NOT NULL DEFAULT 10,
    iteration_confirmation_interval INTEGER NOT NULL DEFAULT 5,
    cli_session_id VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sessions_user ON inkwest.sessions(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX idx_sessions_cluster ON inkwest.sessions(cluster_id);
CREATE INDEX idx_sessions_updated ON inkwest.sessions(updated_at DESC);
CREATE INDEX idx_sessions_active ON inkwest.sessions(is_archived) WHERE is_archived = false;

CREATE TABLE inkwest.messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES inkwest.sessions(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL CHECK (role IN ('user', 'assistant', 'tool_result')),
    content JSONB NOT NULL,
    tool_calls JSONB,
    tool_call_id VARCHAR(255),
    token_count INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    thinking_content TEXT,
    latency_ms INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    error_message TEXT,
    is_retryable BOOLEAN NOT NULL DEFAULT false,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_messages_session ON inkwest.messages(session_id);
CREATE INDEX idx_messages_session_order ON inkwest.messages(session_id, created_at ASC);
CREATE INDEX idx_messages_status ON inkwest.messages(status) WHERE status = 'error';

CREATE TABLE inkwest.role_kubeconfig_mappings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500),
    allowed_clusters UUID[] NOT NULL DEFAULT '{}',
    kubeconfig_override VARCHAR(1024),
    allowed_namespaces VARCHAR[] NOT NULL DEFAULT '{}',
    denied_namespaces VARCHAR[] NOT NULL DEFAULT '{}',
    can_web_search BOOLEAN NOT NULL DEFAULT false,
    daily_token_budget INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_rkm_role_name ON inkwest.role_kubeconfig_mappings(role_name);
CREATE INDEX idx_rkm_active ON inkwest.role_kubeconfig_mappings(is_active) WHERE is_active = true;

CREATE TABLE inkwest.budgets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    budget_type budgettype NOT NULL,
    target_id UUID,
    description VARCHAR(1000),
    target_role VARCHAR(100),
    max_input_tokens INTEGER,
    max_output_tokens INTEGER,
    max_total_tokens INTEGER,
    max_cost_usd NUMERIC(10, 4),
    period budgetperiod NOT NULL DEFAULT 'daily',
    soft_limit_percent INTEGER NOT NULL DEFAULT 80,
    hard_limit_action VARCHAR(50) NOT NULL DEFAULT 'block',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_budgets_type_target ON inkwest.budgets(budget_type, target_id);
CREATE INDEX idx_budgets_active ON inkwest.budgets(is_active) WHERE is_active = true;
CREATE INDEX idx_budgets_target_role ON inkwest.budgets(target_role) WHERE target_role IS NOT NULL;

CREATE TABLE inkwest.usage_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID,
    session_id UUID NOT NULL,
    model VARCHAR(100) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    provider VARCHAR(50) NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_usage_records_user ON inkwest.usage_records(user_id);
CREATE INDEX idx_usage_records_session ON inkwest.usage_records(session_id);
CREATE INDEX idx_usage_records_recorded ON inkwest.usage_records(recorded_at DESC);
CREATE INDEX idx_usage_records_model ON inkwest.usage_records(model);

CREATE TABLE inkwest.oidc_pending_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_oidc_pending_states_created ON inkwest.oidc_pending_states(created_at);

-- migrate:down

DROP TABLE IF EXISTS inkwest.oidc_pending_states;
DROP TABLE IF EXISTS inkwest.usage_records;
DROP TABLE IF EXISTS inkwest.budgets;
DROP TABLE IF EXISTS inkwest.role_kubeconfig_mappings;
DROP TABLE IF EXISTS inkwest.messages;
DROP TABLE IF EXISTS inkwest.sessions;
DROP TABLE IF EXISTS inkwest.users;
DROP TABLE IF EXISTS inkwest.clusters;
DROP TYPE IF EXISTS budgetperiod;
DROP TYPE IF EXISTS budgettype;
DROP EXTENSION IF EXISTS "uuid-ossp";
