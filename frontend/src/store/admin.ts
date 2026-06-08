/**
 * Admin store for budget, user, and role management.
 */

import { create } from 'zustand';
import {
  Budget,
  CreateBudgetRequest,
  UpdateBudgetRequest,
  RoleMapping,
  CreateRoleMappingRequest,
  UpdateRoleMappingRequest,
  User,
  UsageSummary,
  UserUsage,
  ModelUsage,
  CurrentUser,
} from '@/types/admin';
import * as adminApi from '@/services/adminApi';

// ============================================================================
// Auth Store
// ============================================================================

interface AuthState {
  user: CurrentUser | null;
  isAuthenticated: boolean;
  oidcEnabled: boolean;
  loginRedirectMode: 'auto' | 'prompt';
  isLoading: boolean;
  error: string | null;

  checkAuth: () => Promise<void>;
  logout: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  oidcEnabled: false,
  loginRedirectMode: 'auto',
  isLoading: true,
  error: null,

  checkAuth: async () => {
    set({ isLoading: true, error: null });
    try {
      const status = await adminApi.getAuthStatus();
      const base = {
        oidcEnabled: status.oidc_enabled,
        loginRedirectMode: status.login_redirect_mode,
        isLoading: false,
      };
      if (status.authenticated && status.user) {
        set({ ...base, user: status.user, isAuthenticated: true });
      } else {
        set({ ...base, user: null, isAuthenticated: false });
      }
    } catch {
      set({ user: null, isAuthenticated: false, isLoading: false });
    }
  },

  logout: async () => {
    try {
      await adminApi.logout();
    } finally {
      set({ user: null, isAuthenticated: false });
    }
  },

  clearError: () => set({ error: null }),
}));

// ============================================================================
// Budget Store
// ============================================================================

interface BudgetState {
  budgets: Budget[];
  selectedBudget: Budget | null;
  isLoading: boolean;
  error: string | null;

  fetchBudgets: () => Promise<void>;
  createBudget: (data: CreateBudgetRequest) => Promise<Budget>;
  updateBudget: (id: string, data: UpdateBudgetRequest) => Promise<Budget>;
  deleteBudget: (id: string) => Promise<void>;
  selectBudget: (budget: Budget | null) => void;
  clearError: () => void;
}

export const useBudgetStore = create<BudgetState>((set) => ({
  budgets: [],
  selectedBudget: null,
  isLoading: false,
  error: null,

  fetchBudgets: async () => {
    set({ isLoading: true, error: null });
    try {
      const budgets = await adminApi.getBudgets();
      set({ budgets, isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  createBudget: async (data: CreateBudgetRequest) => {
    const budget = await adminApi.createBudget(data);
    set((state) => ({ budgets: [...state.budgets, budget] }));
    return budget;
  },

  updateBudget: async (id: string, data: UpdateBudgetRequest) => {
    const budget = await adminApi.updateBudget(id, data);
    set((state) => ({
      budgets: state.budgets.map((b) => (b.id === id ? budget : b)),
      selectedBudget: state.selectedBudget?.id === id ? budget : state.selectedBudget,
    }));
    return budget;
  },

  deleteBudget: async (id: string) => {
    await adminApi.deleteBudget(id);
    set((state) => ({
      budgets: state.budgets.filter((b) => b.id !== id),
      selectedBudget: state.selectedBudget?.id === id ? null : state.selectedBudget,
    }));
  },

  selectBudget: (budget) => set({ selectedBudget: budget }),

  clearError: () => set({ error: null }),
}));

// ============================================================================
// Role Mapping Store
// ============================================================================

interface RoleMappingState {
  roleMappings: RoleMapping[];
  selectedRoleMapping: RoleMapping | null;
  isLoading: boolean;
  error: string | null;

  fetchRoleMappings: () => Promise<void>;
  createRoleMapping: (data: CreateRoleMappingRequest) => Promise<RoleMapping>;
  updateRoleMapping: (id: string, data: UpdateRoleMappingRequest) => Promise<RoleMapping>;
  deleteRoleMapping: (id: string) => Promise<void>;
  selectRoleMapping: (roleMapping: RoleMapping | null) => void;
  clearError: () => void;
}

export const useRoleMappingStore = create<RoleMappingState>((set) => ({
  roleMappings: [],
  selectedRoleMapping: null,
  isLoading: false,
  error: null,

  fetchRoleMappings: async () => {
    set({ isLoading: true, error: null });
    try {
      const roleMappings = await adminApi.getRoleMappings();
      set({ roleMappings, isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  createRoleMapping: async (data: CreateRoleMappingRequest) => {
    const roleMapping = await adminApi.createRoleMapping(data);
    set((state) => ({ roleMappings: [...state.roleMappings, roleMapping] }));
    return roleMapping;
  },

  updateRoleMapping: async (id: string, data: UpdateRoleMappingRequest) => {
    const roleMapping = await adminApi.updateRoleMapping(id, data);
    set((state) => ({
      roleMappings: state.roleMappings.map((r) => (r.id === id ? roleMapping : r)),
      selectedRoleMapping: state.selectedRoleMapping?.id === id ? roleMapping : state.selectedRoleMapping,
    }));
    return roleMapping;
  },

  deleteRoleMapping: async (id: string) => {
    await adminApi.deleteRoleMapping(id);
    set((state) => ({
      roleMappings: state.roleMappings.filter((r) => r.id !== id),
      selectedRoleMapping: state.selectedRoleMapping?.id === id ? null : state.selectedRoleMapping,
    }));
  },

  selectRoleMapping: (roleMapping) => set({ selectedRoleMapping: roleMapping }),

  clearError: () => set({ error: null }),
}));

// ============================================================================
// User Store
// ============================================================================

interface UserState {
  users: User[];
  selectedUser: User | null;
  isLoading: boolean;
  error: string | null;

  fetchUsers: () => Promise<void>;
  updateUserRoles: (id: string, roles: string[]) => Promise<User>;
  updateUserStatus: (id: string, isActive: boolean) => Promise<User>;
  selectUser: (user: User | null) => void;
  clearError: () => void;
}

export const useUserStore = create<UserState>((set) => ({
  users: [],
  selectedUser: null,
  isLoading: false,
  error: null,

  fetchUsers: async () => {
    set({ isLoading: true, error: null });
    try {
      const users = await adminApi.getUsers();
      set({ users, isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  updateUserRoles: async (id: string, roles: string[]) => {
    const user = await adminApi.updateUserRoles(id, { roles });
    set((state) => ({
      users: state.users.map((u) => (u.id === id ? user : u)),
      selectedUser: state.selectedUser?.id === id ? user : state.selectedUser,
    }));
    return user;
  },

  updateUserStatus: async (id: string, isActive: boolean) => {
    const user = await adminApi.updateUserStatus(id, { is_active: isActive });
    set((state) => ({
      users: state.users.map((u) => (u.id === id ? user : u)),
      selectedUser: state.selectedUser?.id === id ? user : state.selectedUser,
    }));
    return user;
  },

  selectUser: (user) => set({ selectedUser: user }),

  clearError: () => set({ error: null }),
}));

// ============================================================================
// Usage Store
// ============================================================================

interface UsageState {
  summary: UsageSummary | null;
  userUsage: UserUsage[];
  modelUsage: ModelUsage[];
  timeseries: adminApi.UsageTimeseriesPoint[];
  period: string;
  dateRange: adminApi.UsageDateRange;
  isLoading: boolean;
  error: string | null;

  fetchAll: () => Promise<void>;
  setPeriod: (period: string) => void;
  setDateRange: (range: adminApi.UsageDateRange) => void;
  clearError: () => void;
}

function autoGranularity(period: string, dateRange: adminApi.UsageDateRange): 'hour' | 'day' {
  if (period === 'hourly') return 'hour';
  if (period === 'daily') return 'hour';
  if (dateRange.startDate && dateRange.endDate) {
    const days = (new Date(dateRange.endDate).getTime() - new Date(dateRange.startDate).getTime()) / 86_400_000;
    return days <= 2 ? 'hour' : 'day';
  }
  return 'day';
}

export const useUsageStore = create<UsageState>((set, get) => ({
  summary: null,
  userUsage: [],
  modelUsage: [],
  timeseries: [],
  period: 'monthly',
  dateRange: {},
  isLoading: false,
  error: null,

  fetchAll: async () => {
    const { period, dateRange } = get();
    const apiPeriod = period === 'last_month' || period === 'custom' ? 'daily' : period;
    const granularity = autoGranularity(period, dateRange);
    set({ isLoading: true, error: null });
    try {
      const [summary, userUsage, modelUsage, timeseries] = await Promise.all([
        adminApi.getUsageSummary(apiPeriod, dateRange),
        adminApi.getUsageByUser(apiPeriod, 20, dateRange),
        adminApi.getUsageByModel(apiPeriod, dateRange),
        adminApi.getUsageTimeseries(granularity, apiPeriod, dateRange),
      ]);
      set({ summary, userUsage, modelUsage, timeseries, isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  setPeriod: (period) => set({ period }),
  setDateRange: (dateRange) => set({ dateRange }),

  clearError: () => set({ error: null }),
}));

// ============================================================================
// Cluster Store
// ============================================================================

interface ClusterState {
  clusters: adminApi.AdminCluster[];
  selectedCluster: adminApi.AdminCluster | null;
  isLoading: boolean;
  error: string | null;

  fetchClusters: (activeOnly?: boolean) => Promise<void>;
  createCluster: (data: adminApi.CreateClusterRequest) => Promise<adminApi.AdminCluster>;
  updateCluster: (id: string, data: adminApi.UpdateClusterRequest) => Promise<adminApi.AdminCluster>;
  deleteCluster: (id: string) => Promise<void>;
  selectCluster: (cluster: adminApi.AdminCluster | null) => void;
  clearError: () => void;
}

export const useClusterStore = create<ClusterState>((set) => ({
  clusters: [],
  selectedCluster: null,
  isLoading: false,
  error: null,

  fetchClusters: async (activeOnly = false) => {
    set({ isLoading: true, error: null });
    try {
      const clusters = await adminApi.getClusters(activeOnly);
      set({ clusters: Array.isArray(clusters) ? clusters : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  createCluster: async (data: adminApi.CreateClusterRequest) => {
    set({ error: null });
    try {
      const cluster = await adminApi.createCluster(data);
      set((state) => ({ clusters: [...state.clusters, cluster] }));
      return cluster;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  updateCluster: async (id: string, data: adminApi.UpdateClusterRequest) => {
    set({ error: null });
    try {
      const cluster = await adminApi.updateCluster(id, data);
      set((state) => ({
        clusters: state.clusters.map((c) => (c.id === id ? cluster : c)),
        selectedCluster: state.selectedCluster?.id === id ? cluster : state.selectedCluster,
      }));
      return cluster;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  deleteCluster: async (id: string) => {
    await adminApi.deleteCluster(id);
    set((state) => ({
      clusters: state.clusters.filter((c) => c.id !== id),
      selectedCluster: state.selectedCluster?.id === id ? null : state.selectedCluster,
    }));
  },

  selectCluster: (cluster) => set({ selectedCluster: cluster }),

  clearError: () => set({ error: null }),
}));

// ============================================================================
// Prompt Template Store (admin CRUD)
// ============================================================================

interface PromptTemplateState {
  templates: adminApi.AdminPromptTemplate[];
  isLoading: boolean;
  error: string | null;

  fetchTemplates: (activeOnly?: boolean) => Promise<void>;
  createTemplate: (
    data: adminApi.CreatePromptTemplateRequest
  ) => Promise<adminApi.AdminPromptTemplate>;
  updateTemplate: (
    id: string,
    data: adminApi.UpdatePromptTemplateRequest
  ) => Promise<adminApi.AdminPromptTemplate>;
  deleteTemplate: (id: string) => Promise<void>;
  clearError: () => void;
}

export const usePromptTemplateStore = create<PromptTemplateState>((set) => ({
  templates: [],
  isLoading: false,
  error: null,

  fetchTemplates: async (activeOnly = false) => {
    set({ isLoading: true, error: null });
    try {
      const templates = await adminApi.getPromptTemplates(activeOnly);
      set({ templates: Array.isArray(templates) ? templates : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  createTemplate: async (data: adminApi.CreatePromptTemplateRequest) => {
    set({ error: null });
    try {
      const template = await adminApi.createPromptTemplate(data);
      set((state) => ({ templates: [...state.templates, template] }));
      return template;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  updateTemplate: async (id: string, data: adminApi.UpdatePromptTemplateRequest) => {
    set({ error: null });
    try {
      const template = await adminApi.updatePromptTemplate(id, data);
      set((state) => ({
        templates: state.templates.map((t) => (t.id === id ? template : t)),
      }));
      return template;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  deleteTemplate: async (id: string) => {
    await adminApi.deletePromptTemplate(id);
    set((state) => ({
      templates: state.templates.filter((t) => t.id !== id),
    }));
  },

  clearError: () => set({ error: null }),
}));

// ============================================================================
// System Prompt store
// ============================================================================

interface SystemPromptState {
  prompts: adminApi.AdminSystemPrompt[];
  isLoading: boolean;
  error: string | null;

  fetchPrompts: (activeOnly?: boolean) => Promise<void>;
  createPrompt: (
    data: adminApi.CreateSystemPromptRequest
  ) => Promise<adminApi.AdminSystemPrompt>;
  updatePrompt: (
    id: string,
    data: adminApi.UpdateSystemPromptRequest
  ) => Promise<adminApi.AdminSystemPrompt>;
  deletePrompt: (id: string) => Promise<void>;
  clearError: () => void;
}

// Promoting one default demotes the previous one server-side, so refetch keeps
// the single-default invariant visible in the list.
export const useSystemPromptStore = create<SystemPromptState>((set, get) => ({
  prompts: [],
  isLoading: false,
  error: null,

  fetchPrompts: async (activeOnly = false) => {
    set({ isLoading: true, error: null });
    try {
      const prompts = await adminApi.getSystemPrompts(activeOnly);
      set({ prompts: Array.isArray(prompts) ? prompts : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  createPrompt: async (data: adminApi.CreateSystemPromptRequest) => {
    set({ error: null });
    try {
      const prompt = await adminApi.createSystemPrompt(data);
      await get().fetchPrompts();
      return prompt;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  updatePrompt: async (id: string, data: adminApi.UpdateSystemPromptRequest) => {
    set({ error: null });
    try {
      const prompt = await adminApi.updateSystemPrompt(id, data);
      await get().fetchPrompts();
      return prompt;
    } catch (error) {
      set({ error: (error as Error).message });
      throw error;
    }
  },

  deletePrompt: async (id: string) => {
    await adminApi.deleteSystemPrompt(id);
    set((state) => ({
      prompts: state.prompts.filter((p) => p.id !== id),
    }));
  },

  clearError: () => set({ error: null }),
}));
