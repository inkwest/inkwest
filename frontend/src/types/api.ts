// Cluster types
export interface Cluster {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  is_active: boolean;
  created_at: string;
}

export interface ClusterSummary {
  id: string;
  name: string;
  display_name: string;
}

// Skill types
export interface Skill {
  id: string;
  name: string;
  // Server-derived `/<slug>` skill trigger (slugify of the name's base); see backend.
  slug: string;
  // Server-derived argument hint, parsed from a trailing "(...)" in the name.
  argument_hint?: string | null;
  content: string;
  description?: string;
  is_active: boolean;
  created_at: string;
}

// Session types
export interface Session {
  id: string;
  cluster_id?: string | null;
  title?: string;
  cluster?: ClusterSummary | null;
  /** Per-session model; null/undefined = global default. */
  model?: string | null;
  /** Per-session agentic engine pairing; null/undefined = resolve per turn. */
  engine?: string | null;
  total_input_tokens?: number;
  total_output_tokens?: number;
  created_at: string;
  updated_at: string;
}

export interface SessionWithMessages extends Session {
  messages: Message[];
  cluster: Cluster | null;
}

// Message types
export type MessageStatus = 'pending' | 'success' | 'error';

// A file the user attached to a turn. The agent reads it from the conversation's
// /workdir; `filename` is the (slugified, deduped) on-disk basename used by the
// GET/DELETE attachment endpoints.
export interface Attachment {
  filename: string;
  original_name: string;
  content_type: string;
  size: number;
}

export interface Message {
  id: string;
  session_id: string;
  // 'system' = UI-only event line (e.g. a connector access change); never sent to the model.
  role: 'user' | 'assistant' | 'tool_result' | 'system';
  content: MessageContent;
  /** Files attached to this (user) turn; null/undefined when none. */
  attachments?: Attachment[] | null;
  /** Model that produced this assistant turn; null/undefined for older or non-assistant rows. */
  model?: string | null;
  /** Agentic engine that produced this assistant turn; null/undefined for older or non-assistant rows. */
  engine?: string | null;
  status?: MessageStatus;
  error_message?: string;
  is_retryable?: boolean;
  retry_count?: number;
  created_at: string;
  /** Frontend-only: ids of the server rows fused into this bubble for display
   *  (see groupServerMessages). Lets a search deep-link to any absorbed round
   *  still resolve its scroll/highlight target. Absent on un-fused rows. */
  mergedIds?: string[];
}

export type MessageContent = string | ContentBlock[] | SessionEventContent;

// One structured target of a session event. Subject-specific fields are all optional
// so the shape stays generic: AWS uses account_id/account_name/role, MCP uses server,
// model uses model. Stored as data (not a rendered string) so an exported conversation
// carries full machine-readable detail.
export interface SessionEventTarget {
  account_id?: string;
  account_name?: string;
  role?: string;
  server?: string;
  model?: string | null;
}

// Generic session activity event (connector grant/revoke/renew, MCP connect/disconnect,
// model change, …), rendered inline as a faded note. `subject` + `action` drive the
// wording; `targets` carries the structured detail.
export interface SessionEventContent {
  type: 'session_event';
  subject: string;
  action: string;
  targets?: SessionEventTarget[];
}

export interface ContentBlock {
  type: 'text' | 'tool_use' | 'tool_result';
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string;
  is_error?: boolean;
}

// Search types
export interface SearchMessageHit {
  id: string;
  role: string;
  snippet: string;
  created_at: string;
}

export interface SearchSessionHit {
  id: string;
  title: string | null;
  cluster: ClusterSummary | null;
  created_at: string;
  updated_at: string;
  matched_messages: SearchMessageHit[];
  title_match: boolean;
}

export interface SearchResponse {
  query: string;
  results: SearchSessionHit[];
  total: number;
}
