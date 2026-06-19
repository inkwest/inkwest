// WebSocket events
export interface ChatEvent {
  type:
    | 'text_delta'
    | 'tool_start'
    | 'tool_result'
    | 'done'
    | 'error'
    | 'pong'
    | 'stream_resumed'
    | 'connector_required';
  content?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  is_error?: boolean;
  metadata?: Record<string, unknown>;
}

import { Attachment } from './api';

export interface SendMessage {
  type: 'message' | 'ping' | 'stop' | 'retry';
  content?: string;
  // Descriptors from a prior /attachments upload, persisted on the user turn and
  // referenced to the agent as @/workdir/<file> (claude-cli only).
  attachments?: Attachment[];
}
