/**
 * Self-service usage API — the current user's own conversations and costs.
 * Hits `/api/me/usage/*` (scoped server-side to the caller), unlike the
 * cross-user admin reports in adminApi.ts.
 */

import { fetchAPI } from '@/services/api';
import type {
  ModelUsage,
  SessionUsage,
  UsageDateRange,
  UsageSummary,
  UsageTimeseriesPoint,
} from '@/types/admin';

export type { UsageDateRange, UsageTimeseriesPoint };

function usageParams(period: string, range?: UsageDateRange): string {
  const params = new URLSearchParams({ period });
  if (range?.startDate) params.set('start_date', range.startDate);
  if (range?.endDate) params.set('end_date', range.endDate);
  return params.toString();
}

export async function getMyUsageSummary(
  period: string = 'monthly',
  range?: UsageDateRange,
): Promise<UsageSummary> {
  return fetchAPI(`/api/me/usage/summary?${usageParams(period, range)}`);
}

export async function getMyUsageByModel(
  period: string = 'monthly',
  range?: UsageDateRange,
): Promise<ModelUsage[]> {
  return fetchAPI(`/api/me/usage/by-model?${usageParams(period, range)}`);
}

export async function getMyUsageSessions(
  period: string = 'monthly',
  range?: UsageDateRange,
  limit?: number,
): Promise<SessionUsage[]> {
  const params = usageParams(period, range);
  return fetchAPI(`/api/me/usage/sessions?${params}${limit ? `&limit=${limit}` : ''}`);
}

export async function getMyUsageTimeseries(
  granularity: 'hour' | 'day' = 'day',
  period: string = 'monthly',
  range?: UsageDateRange,
): Promise<UsageTimeseriesPoint[]> {
  return fetchAPI(`/api/me/usage/timeseries?granularity=${granularity}&${usageParams(period, range)}`);
}
