// WebSocket events
export interface ChatEvent {
  type:
    | 'text_delta'
    | 'tool_start'
    | 'tool_result'
    | 'done'
    | 'error'
    | 'pong'
    | 'connector_required';
  content?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  is_error?: boolean;
  metadata?: Record<string, unknown>;
}

export interface SendMessage {
  type: 'message' | 'ping' | 'stop';
  content?: string;
}
