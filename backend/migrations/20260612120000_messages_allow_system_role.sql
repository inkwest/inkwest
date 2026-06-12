-- migrate:up

-- The conversation now records connector/MCP access changes as inline 'system'
-- event messages (UI-only; excluded from the model payload). The original CHECK
-- only allowed user/assistant/tool_result, so these inserts were rejected — widen it.
ALTER TABLE inkwest.messages DROP CONSTRAINT IF EXISTS messages_role_check;
ALTER TABLE inkwest.messages
    ADD CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant', 'tool_result', 'system'));

-- migrate:down

-- Drop the UI-only event messages before narrowing the constraint, else the
-- restored CHECK would reject the existing 'system' rows.
DELETE FROM inkwest.messages WHERE role = 'system';
ALTER TABLE inkwest.messages DROP CONSTRAINT IF EXISTS messages_role_check;
ALTER TABLE inkwest.messages
    ADD CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant', 'tool_result'));
