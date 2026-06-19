-- migrate:up

-- Files the user attached to a turn, uploaded into the conversation's /workdir and
-- referenced to the agent as @/workdir/<file>. Stored as a JSON list of descriptors
-- ({filename, original_name, content_type, size}) on the user message so the chat
-- can re-render thumbnails on history reload. NULL for messages without attachments.
ALTER TABLE inkwest.messages ADD COLUMN attachments JSONB;

-- migrate:down

ALTER TABLE inkwest.messages DROP COLUMN IF EXISTS attachments;
