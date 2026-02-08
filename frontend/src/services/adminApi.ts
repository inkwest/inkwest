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
  await fetchAPI('/api/auth/logout', { method: 'POST' });
  localStorage.removeItem('session_token');
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
