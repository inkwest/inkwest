import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as usageApi from '@/services/usageApi';
import { useMyUsageStore } from './usage';
import type { ModelUsage, SessionUsage, UsageSummary } from '@/types/admin';

const SUMMARY: UsageSummary = {
  period: 'monthly',
  period_start: '2026-06-01T00:00:00+00:00',
  total_input_tokens: 5000,
  total_output_tokens: 3000,
  total_tokens: 8000,
  total_cost_usd: 0.12,
  request_count: 10,
};

const SESSIONS: SessionUsage[] = [
  {
    session_id: 's1',
    title: 'Investigate prod outage',
    input_tokens: 4000,
    output_tokens: 2000,
    total_tokens: 6000,
    cost_usd: 0.1,
    request_count: 7,
    first_request: '2026-06-02T08:00:00+00:00',
    last_request: '2026-06-02T09:00:00+00:00',
  },
];

const MODELS: ModelUsage[] = [
  {
    model: 'claude-opus-4-8',
    engine: 'claude-cli',
    input_tokens: 5000,
    output_tokens: 3000,
    total_tokens: 8000,
    cost_usd: 0.12,
    request_count: 10,
  },
];

describe('useMyUsageStore', () => {
  beforeEach(() => {
    useMyUsageStore.setState({
      summary: null,
      sessions: [],
      modelUsage: [],
      timeseries: [],
      period: 'monthly',
      dateRange: {},
      isLoading: false,
      error: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches all four reports in parallel and clears loading on success', async () => {
    vi.spyOn(usageApi, 'getMyUsageSummary').mockResolvedValue(SUMMARY);
    vi.spyOn(usageApi, 'getMyUsageSessions').mockResolvedValue(SESSIONS);
    vi.spyOn(usageApi, 'getMyUsageByModel').mockResolvedValue(MODELS);
    vi.spyOn(usageApi, 'getMyUsageTimeseries').mockResolvedValue([]);

    await useMyUsageStore.getState().fetchAll();

    const s = useMyUsageStore.getState();
    expect(s.summary).toEqual(SUMMARY);
    expect(s.sessions).toEqual(SESSIONS);
    expect(s.modelUsage).toEqual(MODELS);
    expect(s.isLoading).toBe(false);
    expect(s.error).toBeNull();
  });

  it('records the error and stops loading when a fetch fails', async () => {
    vi.spyOn(usageApi, 'getMyUsageSummary').mockRejectedValue(new Error('boom'));
    vi.spyOn(usageApi, 'getMyUsageSessions').mockResolvedValue([]);
    vi.spyOn(usageApi, 'getMyUsageByModel').mockResolvedValue([]);
    vi.spyOn(usageApi, 'getMyUsageTimeseries').mockResolvedValue([]);

    await useMyUsageStore.getState().fetchAll();

    const s = useMyUsageStore.getState();
    expect(s.error).toBe('boom');
    expect(s.isLoading).toBe(false);
    expect(s.summary).toBeNull();
  });
});
