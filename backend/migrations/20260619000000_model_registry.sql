-- migrate:up

-- Admin-managed model registry: the single source of truth for which models
-- users may pick, the default, per-model metadata, and pricing overrides.
--
-- Replaces the two model keys previously kept in app_settings
-- ("anthropic_model" = default, "enabled_models" = JSON array). The live
-- provider catalog (app.services.model_catalog) is still discovered at runtime;
-- discovered models are upserted here as source='discovered' rows so admins can
-- enable them and attach overrides, while source='custom' rows are operator-
-- added models that need explicit endpoint/arn/pricing (e.g. open-weight
-- Bedrock models the discovery filter does not surface).
--
-- enabled/is_default mirror the old semantics: when NO row is enabled the app
-- treats every available model as selectable (fresh-deploy default-open), and
-- the is_default row wins only while it is still enabled and available.
CREATE TABLE inkwest.model_registry (
    model_id VARCHAR(512) PRIMARY KEY,
    -- anthropic | bedrock | bedrock-open | claude-cli — drives chat routing
    -- (open models go through the Bedrock Converse orchestrator) and UI grouping.
    provider VARCHAR(32) NOT NULL,
    -- Label override; NULL = derived from the ID (pricing.model_label).
    display_name VARCHAR(255),
    -- Optional custom inference endpoint (base URL) for gateway-fronted models.
    endpoint VARCHAR(1024),
    -- Optional Bedrock inference-profile / imported-model ARN.
    arn VARCHAR(1024),
    -- Freeform per-model options (region, max_tokens cap, extra headers...).
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Pricing override per million tokens; NULL = fall back to the static
    -- pricing table, then to a conservative default (flagged "unpriced" in UI).
    input_price NUMERIC(12, 4),
    output_price NUMERIC(12, 4),
    enabled BOOLEAN NOT NULL DEFAULT false,
    is_default BOOLEAN NOT NULL DEFAULT false,
    -- discovered (from the live provider catalog) | custom (operator-added).
    source VARCHAR(16) NOT NULL DEFAULT 'discovered',
    -- Older generation: kept for cost reporting, surfaced so admins don't enable it.
    legacy BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT model_registry_source_check CHECK (source IN ('discovered', 'custom')),
    CONSTRAINT model_registry_price_pair CHECK (
        (input_price IS NULL) = (output_price IS NULL)
    )
);

-- At most one default model at a time.
CREATE UNIQUE INDEX idx_model_registry_default
    ON inkwest.model_registry (is_default) WHERE is_default;

CREATE INDEX idx_model_registry_enabled
    ON inkwest.model_registry (enabled) WHERE enabled;

-- migrate:down

DROP TABLE inkwest.model_registry;
