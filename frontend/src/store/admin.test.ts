import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as adminApi from '@/services/adminApi';
import { useAuthStore, useSystemInfoStore } from './admin';
import { LOGOUT_PUSH_TIMEOUT_MS } from '@/services/push';
import { hangUntilAborted, installPushEnv, stubFetch, uninstallPushEnv } from '@/test/pushEnv';

const SAMPLE: adminApi.SystemInfo = {
  app_version: 'v1.2.3',
  app_commit: 'abc1234',
  app_build_timestamp: '2026-06-22T10:00:00+00:00',
  agent_image: 'registry/inkwest/agent:integration-latest',
  app_env: 'production',
  default_model: 'eu.anthropic.claude-opus-4-8',
  engines: ['claude-cli', 'inkwest-agent'],
  oidc_enabled: true,
  sandbox_enabled: true,
  attachments_enabled: false,
  secret_storage_enabled: true,
  catalog: {
    last_refreshed_at: '2026-06-22T09:55:00+00:00',
    source: 'live',
    last_fetch_failed: false,
    error: null,
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

describe('useAuthStore.logout push cleanup', () => {
  const PUSH = '/api/me/push-subscriptions';
  const LOGOUT = '/api/auth/logout';
  const route = (c: { url: string; method: string }) => `${c.method} ${new URL(c.url).pathname}`;

  beforeEach(() => {
    useAuthStore.setState({ isAuthenticated: true });
  });

  afterEach(() => {
    uninstallPushEnv();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('DELETEs this browser subscription and unsubscribes before the session ends (003-AC14)', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 1 });
    const sub = env.state.sub!;
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    await useAuthStore.getState().logout();
    expect(calls.map(route)).toEqual([`DELETE ${PUSH}`, `POST ${LOGOUT}`]);
    expect(sub.unsubscribe).toHaveBeenCalledOnce();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('still logs out when the DELETE fails (003-AC14)', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 1 });
    const { calls } = stubFetch((method) =>
      method === 'DELETE'
        ? new Response(JSON.stringify({ detail: 'boom' }), { status: 500 })
        : new Response(null, { status: 204 }),
    );
    await useAuthStore.getState().logout();
    expect(calls.map(route)).toEqual([`DELETE ${PUSH}`, `POST ${LOGOUT}`]);
    expect(env.state.sub).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('still logs out, within the bound, when the DELETE hangs (003-AC14)', async () => {
    vi.useFakeTimers();
    installPushEnv({ permission: 'granted', subKeyByte: 1 });
    const { calls } = stubFetch((method, init) =>
      method === 'DELETE' ? hangUntilAborted(init) : new Response(null, { status: 204 }),
    );
    const done = useAuthStore.getState().logout();
    await vi.advanceTimersByTimeAsync(LOGOUT_PUSH_TIMEOUT_MS - 1);
    expect(calls.map(route)).toEqual([`DELETE ${PUSH}`]);
    await vi.advanceTimersByTimeAsync(1);
    await done;
    expect(calls.map(route)).toEqual([`DELETE ${PUSH}`, `POST ${LOGOUT}`]);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('sends no DELETE when no service worker was ever registered (003-AC14)', async () => {
    const env = installPushEnv({ permission: 'granted', registered: false });
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    await useAuthStore.getState().logout();
    expect(env.serviceWorker.register).not.toHaveBeenCalled();
    expect(calls.map(route)).toEqual([`POST ${LOGOUT}`]);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('logs out normally where push is unsupported', async () => {
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    await useAuthStore.getState().logout();
    expect(calls.map(route)).toEqual([`POST ${LOGOUT}`]);
  });
});
