/**
 * Admin API service for budget, user, and role management.
 */

import {
  Budget,
  CreateBudgetRequest,
  UpdateBudgetRequest,
  RoleMapping,
  CreateRoleMappingRequest,
  UpdateRoleMappingRequest,
  User,
  UpdateUserRolesRequest,
  UpdateUserStatusRequest,
  UsageSummary,
  UserUsage,
  ModelUsage,
  UserSessionsUsage,
  AuthStatus,
  CurrentUser,
  McpOauthServer,
  CreateMcpOauthServerRequest,
  UpdateMcpOauthServerRequest,
} from '@/types/admin';

import { API_URL, fetchAPI } from '@/services/api';

// ============================================================================
// Auth API
// ============================================================================

export async function getAuthStatus(): Promise<AuthStatus> {
  return fetchAPI('/api/auth/status');
}

export async function getCurrentUser(): Promise<CurrentUser> {
  return fetchAPI('/api/auth/me');
}

export function getLoginUrl(): string {
  return `${API_URL}/api/auth/login`;
}

export async function logout(): Promise<void> {
  // Backend clears the HttpOnly session cookie; nothing to drop client-side.
  await fetchAPI('/api/auth/logout', { method: 'POST' });
}

// ============================================================================
// Budget API
// ============================================================================

export async function getBudgets(): Promise<Budget[]> {
  return fetchAPI('/api/admin/budgets');
}

export async function getBudget(id: string): Promise<Budget> {
  return fetchAPI(`/api/admin/budgets/${id}`);
}

export async function createBudget(data: CreateBudgetRequest): Promise<Budget> {
  return fetchAPI('/api/admin/budgets', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateBudget(id: string, data: UpdateBudgetRequest): Promise<Budget> {
  return fetchAPI(`/api/admin/budgets/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteBudget(id: string): Promise<void> {
  await fetchAPI(`/api/admin/budgets/${id}`, { method: 'DELETE' });
}

// ============================================================================
// Role Mapping API
// ============================================================================

export async function getRoleMappings(): Promise<RoleMapping[]> {
  return fetchAPI('/api/admin/role-mappings');
}

export async function getRoleMapping(id: string): Promise<RoleMapping> {
  return fetchAPI(`/api/admin/role-mappings/${id}`);
}

export async function createRoleMapping(data: CreateRoleMappingRequest): Promise<RoleMapping> {
  return fetchAPI('/api/admin/role-mappings', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateRoleMapping(id: string, data: UpdateRoleMappingRequest): Promise<RoleMapping> {
  return fetchAPI(`/api/admin/role-mappings/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteRoleMapping(id: string): Promise<void> {
  await fetchAPI(`/api/admin/role-mappings/${id}`, { method: 'DELETE' });
}

// ============================================================================
// User API
// ============================================================================

export async function getUsers(): Promise<User[]> {
  return fetchAPI('/api/admin/users');
}

export async function updateUserRoles(id: string, data: UpdateUserRolesRequest): Promise<User> {
  return fetchAPI(`/api/admin/users/${id}/roles`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function updateUserStatus(id: string, data: UpdateUserStatusRequest): Promise<User> {
  return fetchAPI(`/api/admin/users/${id}/status`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

// ============================================================================
// Usage API
// ============================================================================

export interface UsageDateRange {
  startDate?: string;
  endDate?: string;
}

function usageParams(period: string, range?: UsageDateRange): string {
  const params = new URLSearchParams({ period });
  if (range?.startDate) params.set('start_date', range.startDate);
  if (range?.endDate) params.set('end_date', range.endDate);
  return params.toString();
}

export async function getUsageSummary(period: string = 'daily', range?: UsageDateRange): Promise<UsageSummary> {
  return fetchAPI(`/api/admin/usage/summary?${usageParams(period, range)}`);
}

export async function getUsageByUser(
  period: string = 'daily',
  limit: number = 20,
  range?: UsageDateRange,
): Promise<UserUsage[]> {
  return fetchAPI(`/api/admin/usage/by-user?${usageParams(period, range)}&limit=${limit}`);
}

export async function getUsageByModel(period: string = 'daily', range?: UsageDateRange): Promise<ModelUsage[]> {
  return fetchAPI(`/api/admin/usage/by-model?${usageParams(period, range)}`);
}

export async function getUsageByUserSessions(
  userId: string,
  period: string = 'daily',
  range?: UsageDateRange,
): Promise<UserSessionsUsage> {
  return fetchAPI(`/api/admin/usage/by-user/${userId}/sessions?${usageParams(period, range)}`);
}

export interface UsageTimeseriesPoint {
  timestamp: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  request_count: number;
}

export async function getUsageTimeseries(
  granularity: 'hour' | 'day' = 'day',
  period: string = 'daily',
  range?: UsageDateRange,
): Promise<UsageTimeseriesPoint[]> {
  const params = usageParams(period, range);
  return fetchAPI(`/api/admin/usage/timeseries?granularity=${granularity}&${params}`);
}

// ============================================================================
// Model Settings API
// ============================================================================

/** A registry model as the admin console sees it. */
export interface ModelInfo {
  id: string;
  label: string;
  provider: string;
  enabled: boolean;
  is_default: boolean;
  source: 'discovered' | 'custom';
  legacy: boolean;
  /** False = no real pricing; cost tracking uses a conservative default. */
  priced: boolean;
  endpoint: string | null;
  arn: string | null;
  options: Record<string, unknown>;
  /** Engines capable of running this model (which engine sections it appears in). The
   *  engines it is actually offered on live in options.engines (absent = all capable). */
  capable_engines: string[];
  /** Input modalities from the live catalog: TEXT/IMAGE/VIDEO/SPEECH (SPEECH = audio).
   *  Empty = unknown/text-only. Drives the modality icons + capability filter. */
  input_modalities: string[];
  /** Output modalities: TEXT/EMBEDDING/IMAGE/VIDEO. A non-TEXT-only output (embedding /
   *  image-gen) marks a non-chat model (badged + excluded by the chat-capable filter). */
  output_modalities: string[];
  input_price: number | null;
  output_price: number | null;
}

export interface CatalogStatus {
  last_refreshed_at: string | null;
  source: 'live' | 'static';
  last_fetch_failed: boolean;
  count: number;
}

export interface ModelSettings {
  provider: string;
  default_model: string;
  /** The engine the default model runs on (admin choice, else resolved heuristically). */
  default_engine: string;
  /** Model IDs users may pick per session (admin-enabled subset). */
  enabled_models: string[];
  /** Whether all Bedrock vendors (not just Anthropic) are surfaced. */
  include_all_vendors: boolean;
  /** Whether the custom (inkwest-agent) engine is enabled — gates the toggle. */
  inkwest_agent_enabled: boolean;
  /** Engines enabled in this deployment — one admin section per engine. */
  engines: string[];
  catalog: CatalogStatus;
  /** The full merged registry: discovered models + custom rows. */
  models: ModelInfo[];
}

export interface UpdateModelSettingsRequest {
  default_model: string;
  /** The engine the default model runs on; omit/empty = resolve heuristically. */
  default_engine?: string;
  enabled_models: string[];
  /** Per-(engine, model) offering: model_id -> engine ids it's exposed on. */
  engine_availability?: Record<string, string[]>;
}

export interface AddModelRequest {
  id: string;
  provider: string;
  display_name?: string | null;
  endpoint?: string | null;
  arn?: string | null;
  options?: Record<string, unknown>;
  input_price?: number | null;
  output_price?: number | null;
  enabled?: boolean;
  legacy?: boolean;
}

export interface UpdateModelRequest {
  display_name?: string | null;
  endpoint?: string | null;
  arn?: string | null;
  options?: Record<string, unknown>;
  input_price?: number | null;
  output_price?: number | null;
  legacy?: boolean;
  enabled?: boolean;
}

export async function getModelSettings(): Promise<ModelSettings> {
  return fetchAPI('/api/admin/settings/model');
}

export async function updateModelSettings(
  data: UpdateModelSettingsRequest
): Promise<ModelSettings> {
  return fetchAPI('/api/admin/settings/model', {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export interface SandboxSettings {
  /** Effective per-user concurrent-sandbox cap (admin override or env default). 0 = unlimited. */
  max_sessions_per_user: number;
  /** The env baseline, so the UI can show when an override differs from it. */
  default_max_sessions_per_user: number;
  /** Busy-burst ceiling (cap + max(2, 25%)): beyond it a new turn is refused. 0 when unlimited. */
  hard_ceiling: number;
}

export async function getSandboxSettings(): Promise<SandboxSettings> {
  return fetchAPI('/api/admin/settings/sandbox');
}

export async function updateSandboxSettings(
  maxSessionsPerUser: number
): Promise<SandboxSettings> {
  return fetchAPI('/api/admin/settings/sandbox', {
    method: 'PUT',
    body: JSON.stringify({ max_sessions_per_user: maxSessionsPerUser }),
  });
}

/** Force a live catalog refresh; optionally flip the all-vendors flag first. */
export async function refreshModels(includeAllVendors?: boolean): Promise<ModelSettings> {
  return fetchAPI('/api/admin/settings/model/refresh', {
    method: 'POST',
    body: JSON.stringify(
      includeAllVendors === undefined ? {} : { include_all_vendors: includeAllVendors }
    ),
  });
}

export async function addModel(data: AddModelRequest): Promise<ModelSettings> {
  return fetchAPI('/api/admin/models', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateModel(
  modelId: string,
  data: UpdateModelRequest
): Promise<ModelSettings> {
  return fetchAPI(`/api/admin/models/${encodeURIComponent(modelId)}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteModel(modelId: string): Promise<ModelSettings> {
  return fetchAPI(`/api/admin/models/${encodeURIComponent(modelId)}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// Cluster API
// ============================================================================

export interface AdminCluster {
  id: string;
  name: string;
  display_name: string;
  kubeconfig_path: string;
  context_name?: string;
  description?: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateClusterRequest {
  name: string;
  display_name: string;
  kubeconfig_path: string;
  context_name?: string;
  description?: string;
}

export interface UpdateClusterRequest {
  display_name?: string;
  kubeconfig_path?: string;
  context_name?: string;
  description?: string;
  is_active?: boolean;
}

interface ClusterListResponse {
  items: AdminCluster[];
  total: number;
}

export async function getClusters(activeOnly: boolean = false): Promise<AdminCluster[]> {
  const response = await fetchAPI<ClusterListResponse>(`/api/clusters?active_only=${activeOnly}`);
  return response.items;
}

export async function getCluster(id: string): Promise<AdminCluster> {
  return fetchAPI(`/api/clusters/${id}`);
}

export async function createCluster(data: CreateClusterRequest): Promise<AdminCluster> {
  return fetchAPI('/api/clusters', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateCluster(id: string, data: UpdateClusterRequest): Promise<AdminCluster> {
  return fetchAPI(`/api/clusters/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function deleteCluster(id: string): Promise<void> {
  await fetchAPI(`/api/clusters/${id}`, { method: 'DELETE' });
}

// ============================================================================
// Skill API
// ============================================================================

export interface AdminSkill {
  id: string;
  name: string;
  content: string;
  description?: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateSkillRequest {
  // Full SKILL.md (frontmatter + body); name/description/arguments parsed server-side.
  content: string;
}

export interface UpdateSkillRequest {
  content?: string;
  is_active?: boolean;
}

interface SkillListResponse {
  items: AdminSkill[];
  total: number;
}

export async function getSkills(
  activeOnly: boolean = false
): Promise<AdminSkill[]> {
  const response = await fetchAPI<SkillListResponse>(
    `/api/skills?active_only=${activeOnly}`
  );
  return response.items;
}

export async function createSkill(
  data: CreateSkillRequest
): Promise<AdminSkill> {
  return fetchAPI('/api/skills', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateSkill(
  id: string,
  data: UpdateSkillRequest
): Promise<AdminSkill> {
  return fetchAPI(`/api/skills/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function deleteSkill(id: string): Promise<void> {
  await fetchAPI(`/api/skills/${id}`, { method: 'DELETE' });
}

// ============================================================================
// System Prompt API
// ============================================================================

export interface AdminSystemPrompt {
  id: string;
  name: string;
  content: string;
  description?: string;
  is_active: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateSystemPromptRequest {
  name: string;
  content: string;
  description?: string;
  is_default?: boolean;
}

export interface UpdateSystemPromptRequest {
  name?: string;
  content?: string;
  description?: string;
  is_active?: boolean;
  is_default?: boolean;
}

interface SystemPromptListResponse {
  items: AdminSystemPrompt[];
  total: number;
}

export async function getSystemPrompts(
  activeOnly: boolean = false
): Promise<AdminSystemPrompt[]> {
  const response = await fetchAPI<SystemPromptListResponse>(
    `/api/system-prompts?active_only=${activeOnly}`
  );
  return response.items;
}

export async function createSystemPrompt(
  data: CreateSystemPromptRequest
): Promise<AdminSystemPrompt> {
  return fetchAPI('/api/system-prompts', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateSystemPrompt(
  id: string,
  data: UpdateSystemPromptRequest
): Promise<AdminSystemPrompt> {
  return fetchAPI(`/api/system-prompts/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function deleteSystemPrompt(id: string): Promise<void> {
  await fetchAPI(`/api/system-prompts/${id}`, { method: 'DELETE' });
}

// ============================================================================
// System Info API
// ============================================================================

/** Read-only build + deployment info shown on the admin System Info page. */
export interface SystemInfo {
  /** App/API image (combined prod image: backend + baked SPA), from build-time env. */
  app_version: string;
  app_commit: string;
  app_build_timestamp: string;
  /** Image ref the broker spawns the inkwest-agent in; '' when sandbox not configured. */
  agent_image: string;
  app_env: string;
  ai_provider: string;
  default_model: string;
  /** Agentic engines live in this deployment (AGENTIC_ENGINES). */
  engines: string[];
  oidc_enabled: boolean;
  sandbox_enabled: boolean;
  attachments_enabled: boolean;
  /** At-rest encryption for operator secrets configured (SECRETBOX_KEYS). */
  secret_storage_enabled: boolean;
  /** Model/pricing discovery freshness (reuses the model-settings catalog shape). */
  catalog: CatalogStatus;
}

export async function getSystemInfo(): Promise<SystemInfo> {
  return fetchAPI('/api/admin/system-info');
}

// ============================================================================
// OAuth2 MCP Catalog API
// ============================================================================

export interface McpOauthMeta {
  /** At-rest secret encryption configured server-side (SECRETBOX_KEYS). When false, the
   *  Confidential auth type is disabled (no way to store a client secret). */
  secret_storage_enabled: boolean;
}

export async function getMcpOauthMeta(): Promise<McpOauthMeta> {
  return fetchAPI('/api/admin/mcp-oauth/meta');
}

export async function getMcpOauthServers(): Promise<McpOauthServer[]> {
  return fetchAPI('/api/admin/mcp-oauth');
}

export async function createMcpOauthServer(
  data: CreateMcpOauthServerRequest,
): Promise<McpOauthServer[]> {
  return fetchAPI('/api/admin/mcp-oauth', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateMcpOauthServer(
  key: string,
  data: UpdateMcpOauthServerRequest,
): Promise<McpOauthServer[]> {
  return fetchAPI(`/api/admin/mcp-oauth/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteMcpOauthServer(key: string): Promise<McpOauthServer[]> {
  return fetchAPI(`/api/admin/mcp-oauth/${encodeURIComponent(key)}`, {
    method: 'DELETE',
  });
}
