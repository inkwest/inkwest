import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as adminApi from '@/services/adminApi';
import { useSystemInfoStore } from './admin';

const SAMPLE: adminApi.SystemInfo = {
  app_version: 'v1.2.3',
  app_commit: 'abc1234',
  app_build_timestamp: '2026-06-22T10:00:00+00:00',
  agent_image: 'registry/inkwest/agent:integration-latest',
  app_env: 'production',
  ai_provider: 'bedrock',
  default_model: 'eu.anthropic.claude-opus-4-8',
  engines: ['claude-cli', 'inkwest-agent'],
  oidc_enabled: true,
  sandbox_enabled: true,
  attachments_enabled: false,
  catalog: {
    last_refreshed_at: '2026-06-22T09:55:00+00:00',
    source: 'live',
    last_fetch_failed: false,
    count: 7,
  },
};

describe('useSystemInfoStore', () => {
  beforeEach(() => {
    useSystemInfoStore.setState({ info: null, isLoading: false, error: null });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads system info and clears the loading flag on success', async () => {
    vi.spyOn(adminApi, 'getSystemInfo').mockResolvedValue(SAMPLE);

    await useSystemInfoStore.getState().fetchSystemInfo();

    const s = useSystemInfoStore.getState();
    expect(s.info).toEqual(SAMPLE);
    expect(s.isLoading).toBe(false);
    expect(s.error).toBeNull();
  });

  it('records the error and stops loading when the fetch fails', async () => {
    vi.spyOn(adminApi, 'getSystemInfo').mockRejectedValue(new Error('boom'));

    await useSystemInfoStore.getState().fetchSystemInfo();

    const s = useSystemInfoStore.getState();
    expect(s.error).toBe('boom');
    // Regression guard: a failed load must clear isLoading. The page gates the spinner on
    // isLoading only, so leaving it true would spin forever instead of showing the error.
    expect(s.isLoading).toBe(false);
    expect(s.info).toBeNull();
  });

  it('clearError resets the error', () => {
    useSystemInfoStore.setState({ error: 'previous' });
    useSystemInfoStore.getState().clearError();
    expect(useSystemInfoStore.getState().error).toBeNull();
  });
});
