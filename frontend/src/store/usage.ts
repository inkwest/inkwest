/**
 * Self-service usage store — the current user's own usage report.
 * Backed by /api/me/usage (see usageApi.ts); independent of the admin store.
 */

import { create } from 'zustand';
import * as usageApi from '@/services/usageApi';
import type { ModelUsage, SessionUsage, UsageSummary } from '@/types/admin';

// Cap on the per-conversation breakdown — single source of truth for both the
// request and the "showing top N" note the page renders on truncation.
export const MY_USAGE_SESSIONS_LIMIT = 100;

interface MyUsageState {
  summary: UsageSummary | null;
  sessions: SessionUsage[];
  modelUsage: ModelUsage[];
  timeseries: usageApi.UsageTimeseriesPoint[];
  period: string;
  dateRange: usageApi.UsageDateRange;
  isLoading: boolean;
  error: string | null;

  fetchAll: () => Promise<void>;
  setPeriod: (period: string) => void;
  setDateRange: (range: usageApi.UsageDateRange) => void;
  clearError: () => void;
}

function autoGranularity(period: string, dateRange: usageApi.UsageDateRange): 'hour' | 'day' {
  if (period === 'hourly' || period === 'daily') return 'hour';
  if (dateRange.startDate && dateRange.endDate) {
    const days = (new Date(dateRange.endDate).getTime() - new Date(dateRange.startDate).getTime()) / 86_400_000;
    return days <= 2 ? 'hour' : 'day';
  }
  return 'day';
}

export const useMyUsageStore = create<MyUsageState>((set, get) => ({
  summary: null,
  sessions: [],
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
      const [summary, sessions, modelUsage, timeseries] = await Promise.all([
        usageApi.getMyUsageSummary(apiPeriod, dateRange),
        usageApi.getMyUsageSessions(apiPeriod, dateRange, MY_USAGE_SESSIONS_LIMIT),
        usageApi.getMyUsageByModel(apiPeriod, dateRange),
        usageApi.getMyUsageTimeseries(granularity, apiPeriod, dateRange),
      ]);
      set({ summary, sessions, modelUsage, timeseries, isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false });
    }
  },

  setPeriod: (period) => set({ period }),
  setDateRange: (dateRange) => set({ dateRange }),
  clearError: () => set({ error: null }),
}));
