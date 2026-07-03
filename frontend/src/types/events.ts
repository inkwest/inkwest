import { Attachment } from './api';

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
    | 'connector_required'
    | 'mcp_auth_required'
    // Runtime tool-permission gate: the proxy holds a tool call on the user's decision.
    // Targeted frames (only the asking user's sockets), not session broadcasts.
    | 'mcp_tool_ask'
    | 'mcp_tool_ask_resolved'
    // Local lifecycle frames the service synthesizes for the store:
    | 'connected'
    | 'disconnected'
    | 'reconnecting' // a retry is pending (state:true) / retries exhausted (state:false)
    // Collaboration frames (broadcast to all of a session's sockets):
    | 'presence' // who is currently viewing
    | 'presence_self' // (shared view) tells an anonymous guest its own presence id
    | 'typing' // a participant's typing state
    | 'user_message'; // a peer (the owner) sent a turn — echo it for read-only viewers
  content?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  is_error?: boolean;
  metadata?: Record<string, unknown>;
  // presence
  users?: Array<{ user_id: string; display_name: string }>;
  // typing
  user_id?: string;
  display_name?: string;
  state?: boolean;
  // user_message
  attachments?: Attachment[] | null;
  author_user_id?: string;
  author_display_name?: string;
  // mcp_tool_ask / mcp_tool_ask_resolved
  ask_id?: string;
  mcp_key?: string;
  tool?: string;
  verdict?: string;
  // mcp_tool_ask: the conversation the held call belongs to
  session_id?: string;
}

export interface SendMessage {
  type: 'message' | 'ping' | 'stop' | 'retry' | 'typing';
  content?: string;
  // Descriptors from a prior /attachments upload, persisted on the user turn and
  // referenced to the agent as @/workdir/<file> (claude-cli only).
  attachments?: Attachment[];
  // typing
  state?: boolean;
}
