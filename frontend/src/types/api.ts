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

// Personal (per-user) skill — mirrors Skill plus the override provenance fields.
export interface UserSkill {
  id: string;
  name: string;
  slug: string;
  argument_hint?: string | null;
  content: string;
  description?: string | null;
  is_active: boolean;
  // True when this personal skill shadows an admin skill of the same name (badge).
  is_override: boolean;
  source_admin_skill_id?: string | null;
  created_at: string;
  updated_at: string;
}

// UI settings carried in the preferences document (migrated off localStorage).
export interface UiPreferences {
  compactToolCards?: boolean;
  showTimestamps?: boolean;
  /** Per-bubble token line (in/out/cache) next to the timestamp. */
  showTokenCounts?: boolean;
  wideComposer?: boolean;
  sidebarWidth?: number;
}

// The versioned per-user preferences document. The backend only interprets
// `skills.disabled_admin`; `ui` is opaque storage owned by the frontend.
export interface PreferencesDocument {
  ui?: UiPreferences;
  skills?: { disabled_admin?: string[] };
}

export interface PreferencesResponse {
  schema_version: number;
  document: PreferencesDocument;
  skills: UserSkill[];
}

export interface ExportedSkill {
  name: string;
  content: string;
  is_active: boolean;
  is_override: boolean;
}

// The downloadable/importable config envelope (GET /api/me/export shape).
export interface ConfigExport {
  schema_version: number;
  exported_at: string;
  preferences: PreferencesDocument;
  skills: ExportedSkill[];
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
  /** Cumulative prompt-cache tokens (input-side; total_input_tokens is FRESH input only). */
  total_cache_read_tokens?: number;
  total_cache_write_tokens?: number;
  created_at: string;
  updated_at: string;
  /** Collaboration markers (see backend SessionResponse). */
  is_owner?: boolean;
  /** 'viewer' when this is a session shared *with* the user; null/undefined when owned. */
  shared_role?: string | null;
  /** Display name of the owner, for the "shared by X" hint on shared-in sessions. */
  owner_display_name?: string | null;
  /** How many people + active links this conversation is shared with (owner view). */
  share_count?: number;
  /** Timestamp of the newest message; drives the "last activity" sort. Null when empty. */
  last_message_at?: string | null;
}

// Conversation sharing
export interface UserSummary {
  id: string;
  display_name: string;
  email: string;
}

export interface Participant {
  id: string;
  session_id: string;
  user_id: string;
  display_name: string;
  email: string;
  role: string;
  status: 'pending' | 'accepted' | 'declined';
  created_at: string;
  responded_at?: string | null;
}

export interface PendingInvite {
  share_id: string;
  session_id: string;
  session_title?: string | null;
  role: string;
  invited_by_display_name?: string | null;
  created_at: string;
}

export interface ShareLink {
  id: string;
  token: string;
  /** Same-origin path of the public viewer; prepend window.location.origin to share. */
  url: string;
  label?: string | null;
  is_active: boolean;
  created_at: string;
}

export interface SharedConversation {
  session_id: string;
  title?: string | null;
  owner_display_name?: string | null;
  messages: Message[];
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
  /** Per-turn token counts (assistant rows; the turn's first assistant row carries them).
   *  input_tokens is FRESH input — cache tokens are reported separately, input-side only. */
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
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
  /** Abort marker (subject 'turn'): why the turn ended abnormally. */
  reason?: string;
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
