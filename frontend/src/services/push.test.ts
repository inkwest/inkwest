import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  disablePush,
  disablePushForLogout,
  enablePush,
  isPushSupported,
  LOGOUT_PUSH_TIMEOUT_MS,
  syncPushSubscription,
} from './push';
import {
  ENDPOINT,
  hangUntilAborted,
  installPushEnv,
  stubFetch,
  uninstallPushEnv,
  vapidKey,
} from '@/test/pushEnv';

const KEY = vapidKey(0xfb);
const KEY_BYTES = new Uint8Array(65).fill(0xfb);
const ok = () => new Response(null, { status: 204 });
const SUB_JSON = { endpoint: ENDPOINT, expirationTime: null, keys: { p256dh: 'p256', auth: 'auth' } };

afterEach(() => {
  uninstallPushEnv();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('isPushSupported', () => {
  it('is false without serviceWorker / PushManager / Notification', () => {
    expect(isPushSupported()).toBe(false);
  });

  it('is false when only Notification is missing', () => {
    installPushEnv();
    delete (window as { Notification?: unknown }).Notification;
    expect(isPushSupported()).toBe(false);
  });
});

describe('enablePush', () => {
  it('subscribes with the decoded VAPID key and PUTs toJSON()', async () => {
    const env = installPushEnv();
    const { calls } = stubFetch(ok);
    await enablePush(KEY);
    expect(env.notification.requestPermission).toHaveBeenCalledOnce();
    const opts = env.pushManager.subscribe.mock.calls[0][0];
    expect(opts.userVisibleOnly).toBe(true);
    expect(Array.from(opts.applicationServerKey)).toEqual(Array.from(KEY_BYTES));
    expect(calls).toEqual([
      { url: expect.stringContaining('/api/me/push-subscriptions'), method: 'PUT', body: SUB_JSON },
    ]);
  });

  it('subscribes nothing when permission is refused', async () => {
    const env = installPushEnv({ grant: 'denied' });
    const { calls } = stubFetch(ok);
    await expect(enablePush(KEY)).rejects.toThrow(/not granted/);
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });

  it('drops the browser subscription when the PUT fails', async () => {
    const env = installPushEnv();
    stubFetch(() => new Response(JSON.stringify({ detail: 'bad endpoint' }), { status: 422 }));
    await expect(enablePush(KEY)).rejects.toThrow('bad endpoint');
    expect(env.state.sub).toBeNull();
  });
});

describe('disablePush', () => {
  it('DELETEs the endpoint then unsubscribes', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    const sub = env.state.sub!;
    const { calls } = stubFetch(ok);
    await disablePush();
    expect(calls).toEqual([
      { url: expect.stringContaining('/api/me/push-subscriptions'), method: 'DELETE', body: { endpoint: ENDPOINT } },
    ]);
    expect(sub.unsubscribe).toHaveBeenCalledOnce();
    expect(env.serviceWorker.register).not.toHaveBeenCalled();
  });

  it('tolerates a 404 (row already gone)', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    stubFetch(() => new Response(JSON.stringify({ detail: 'nope' }), { status: 404 }));
    await expect(disablePush()).resolves.toBeUndefined();
    expect(env.state.sub).toBeNull();
  });

  it('still unsubscribes when the DELETE fails, and reports the failure', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    stubFetch(() => new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }));
    await expect(disablePush()).rejects.toThrow('boom');
    expect(env.state.sub).toBeNull();
  });

  it('is a no-op, without registering, when no worker was ever registered', async () => {
    const env = installPushEnv({ permission: 'granted', registered: false });
    const { calls } = stubFetch(ok);
    await expect(disablePush()).resolves.toBeUndefined();
    expect(env.serviceWorker.register).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });

  it('is a no-op without a subscription', async () => {
    installPushEnv({ permission: 'granted' });
    const { calls } = stubFetch(ok);
    await disablePush();
    expect(calls).toEqual([]);
  });
});

describe('disablePushForLogout', () => {
  it('aborts a hung DELETE at the bound, still unsubscribes, never throws', async () => {
    vi.useFakeTimers();
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    stubFetch((_m, init) => hangUntilAborted(init));
    let settled = false;
    const p = disablePushForLogout().then(() => (settled = true));
    await vi.advanceTimersByTimeAsync(LOGOUT_PUSH_TIMEOUT_MS - 1);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await p;
    expect(settled).toBe(true);
    expect(env.state.sub).toBeNull();
  });

  it('resolves at the bound even when the browser never finishes unsubscribing', async () => {
    vi.useFakeTimers();
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    env.state.sub!.unsubscribe = vi.fn(() => new Promise<boolean>(() => {}));
    stubFetch(ok);
    let settled = false;
    const p = disablePushForLogout().then(() => (settled = true));
    await vi.advanceTimersByTimeAsync(LOGOUT_PUSH_TIMEOUT_MS);
    await p;
    expect(settled).toBe(true);
  });

  it('does nothing when push is unsupported', async () => {
    const { calls } = stubFetch(ok);
    await disablePushForLogout();
    expect(calls).toEqual([]);
  });
});

describe('syncPushSubscription (self-heal)', () => {
  it('re-PUTs the existing subscription when the key matches', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
    const sub = env.state.sub!;
    const { calls } = stubFetch(ok);
    await syncPushSubscription(KEY);
    expect(sub.unsubscribe).not.toHaveBeenCalled();
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(calls.map((c) => [c.method, c.body])).toEqual([['PUT', SUB_JSON]]);
  });

  it('resubscribes with the new key and PUTs on a VAPID key change', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0x01 });
    const stale = env.state.sub!;
    const { calls } = stubFetch(ok);
    await syncPushSubscription(KEY);
    expect(stale.unsubscribe).toHaveBeenCalledOnce();
    expect(Array.from(env.pushManager.subscribe.mock.calls[0][0].applicationServerKey)).toEqual(
      Array.from(KEY_BYTES),
    );
    // Old row DELETEd first so it stops holding a cap slot.
    expect(calls.map((c) => [c.method, c.body])).toEqual([
      ['DELETE', { endpoint: ENDPOINT }],
      ['PUT', SUB_JSON],
    ]);
  });

  it('still resubscribes when DELETEing the stale row fails', async () => {
    const env = installPushEnv({ permission: 'granted', subKeyByte: 0x01 });
    const stale = env.state.sub!;
    const { calls } = stubFetch((method) =>
      method === 'DELETE' ? new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }) : ok(),
    );
    await syncPushSubscription(KEY);
    expect(stale.unsubscribe).toHaveBeenCalledOnce();
    expect(env.pushManager.subscribe).toHaveBeenCalledOnce();
    expect(calls.map((c) => c.method)).toEqual(['DELETE', 'PUT']);
  });

  it('resubscribes when the stored key has a different length', async () => {
    const env = installPushEnv({ permission: 'granted' });
    env.state.sub = {
      endpoint: ENDPOINT,
      options: { applicationServerKey: KEY_BYTES.slice(0, 64).buffer, userVisibleOnly: true },
      toJSON: () => SUB_JSON,
      unsubscribe: vi.fn(async () => true),
    };
    stubFetch(ok);
    await syncPushSubscription(KEY);
    expect(env.pushManager.subscribe).toHaveBeenCalledOnce();
  });

  it.each(['default', 'denied'] as const)('does nothing when permission is %s', async (permission) => {
    const env = installPushEnv({ permission, subKeyByte: 0xfb });
    const { calls } = stubFetch(ok);
    await syncPushSubscription(KEY);
    expect(env.serviceWorker.register).not.toHaveBeenCalled();
    expect(env.notification.requestPermission).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });

  it('never creates a subscription the user did not turn on', async () => {
    const env = installPushEnv({ permission: 'granted' });
    const { calls } = stubFetch(ok);
    await syncPushSubscription(KEY);
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });
});
