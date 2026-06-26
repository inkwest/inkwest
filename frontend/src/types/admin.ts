// Admin types for budget, user, and role management

// ============================================================================
// User Types
// ============================================================================

export interface User {
  id: string;
  oidc_subject: string;
  oidc_issuer: string;
  email: string;
  display_name: string;
  roles: string[];
  is_active: boolean;
  last_login: string | null;
  created_at: string;
  updated_at: string;
}

export interface UpdateUserRolesRequest {
  roles: string[];
}

export interface UpdateUserStatusRequest {
  is_active: boolean;
}

// ============================================================================
// Budget Types
// ============================================================================

export type BudgetType = 'global' | 'user' | 'role';
export type BudgetPeriod = 'hourly' | 'daily' | 'weekly' | 'monthly';

export interface Budget {
  id: string;
  name: string;
  description: string | null;
  budget_type: BudgetType;
  target_id: string | null;
  target_role: string | null;
  max_total_tokens: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  max_cost_usd: number | null;
  period: BudgetPeriod;
  soft_limit_percent: number;
  hard_limit_action: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateBudgetRequest {
  name: string;
  description?: string;
  budget_type: BudgetType;
  target_id?: string;
  target_role?: string;
  max_total_tokens?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  max_cost_usd?: number;
  period?: BudgetPeriod;
  soft_limit_percent?: number;
  hard_limit_action?: string;
}

export interface UpdateBudgetRequest {
  name?: string;
  description?: string;
  max_total_tokens?: number | null;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  max_cost_usd?: number | null;
  period?: BudgetPeriod;
  soft_limit_percent?: number;
  hard_limit_action?: string;
  is_active?: boolean;
}

// ============================================================================
// Role Mapping Types
// ============================================================================

export interface RoleMapping {
  id: string;
  role_name: string;
  description: string | null;
  allowed_clusters: string[];
  allowed_namespaces: string[];
  denied_namespaces: string[];
  can_web_search: boolean;
  daily_token_budget: number | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateRoleMappingRequest {
  role_name: string;
  description?: string;
  allowed_clusters?: string[];
  allowed_namespaces?: string[];
  denied_namespaces?: string[];
  can_web_search?: boolean;
  daily_token_budget?: number;
}

export interface UpdateRoleMappingRequest {
  description?: string;
  allowed_clusters?: string[];
  allowed_namespaces?: string[];
  denied_namespaces?: string[];
  can_web_search?: boolean;
  daily_token_budget?: number | null;
  is_active?: boolean;
}

// ============================================================================
// Usage Types
// ============================================================================

export interface UsageSummary {
  period: string;
  period_start: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  request_count: number;
}

export interface UserUsage {
  user_id: string;
  email: string;
  display_name: string;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface ModelUsage {
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  request_count: number;
}

export interface SessionUsage {
  session_id: string;
  title: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  request_count: number;
  first_request: string | null;
  last_request: string | null;
}

export interface UserSessionsUsage {
  user_id: string;
  display_name: string;
  email: string;
  sessions: SessionUsage[];
}

// Shared by the admin reports (adminApi.ts) and the self-service reports
// (usageApi.ts) — neutral home so neither client depends on the other.
export interface UsageDateRange {
  startDate?: string;
  endDate?: string;
}

export interface UsageTimeseriesPoint {
  timestamp: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  request_count: number;
}

// ============================================================================
// Auth Types
// ============================================================================

export interface AuthStatus {
  authenticated: boolean;
  oidc_enabled: boolean;
  login_redirect_mode: 'auto' | 'prompt';
  user?: CurrentUser;
}

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  roles: string[];
  oidc_issuer: string;
  is_admin: boolean;
}

// ============================================================================
// OAuth2 MCP Catalog
// ============================================================================

export interface McpOauthServer {
  key: string;
  display_name: string | null;
  url: string;
  scopes: string[];
  client_id: string | null;
  // The raw client_secret is never sent to the browser; this only reports presence.
  has_secret: boolean;
  // False when a sealed secret can't be decrypted with the current server keyring — the
  // panel blocks editing (the server is already out of service); delete/recreate only.
  editable: boolean;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface CreateMcpOauthServerRequest {
  key: string;
  url: string;
  display_name?: string | null;
  scopes?: string[];
  client_id?: string | null;
  client_secret?: string | null;
  enabled?: boolean;
}

export interface UpdateMcpOauthServerRequest {
  url: string;
  display_name?: string | null;
  scopes?: string[];
  client_id?: string | null;
  // Omit to keep the stored secret; send "" to clear it.
  client_secret?: string | null;
  enabled?: boolean;
}
