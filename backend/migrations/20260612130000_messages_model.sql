-- migrate:up

-- Per-message model: the model that actually produced this assistant turn,
-- captured at generation time. Sits alongside the existing per-turn metadata
-- (input_tokens/output_tokens/latency_ms) so cost can be attributed accurately
-- per turn — the session-level model can change mid-conversation, so it cannot
-- reconstruct what an older message ran on. NULL for pre-existing rows and for
-- non-assistant messages.
ALTER TABLE inkwest.messages ADD COLUMN model VARCHAR(255);

-- migrate:down

ALTER TABLE inkwest.messages DROP COLUMN IF EXISTS model;
