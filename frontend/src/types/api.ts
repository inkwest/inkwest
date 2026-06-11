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

// Prompt template types
export interface PromptTemplate {
  id: string;
  name: string;
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

export interface Message {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'tool_result';
  content: MessageContent;
  status?: MessageStatus;
  error_message?: string;
  is_retryable?: boolean;
  retry_count?: number;
  created_at: string;
}

export type MessageContent = string | ContentBlock[];

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
