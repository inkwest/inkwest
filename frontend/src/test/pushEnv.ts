import { vi } from 'vitest';

// jsdom has no serviceWorker / PushManager / Notification: this fakes just what services/push.ts uses.

export const ENDPOINT = 'https://fcm.googleapis.com/fcm/send/test-endpoint';

/** base64url of a 65-byte key filled with `byte` (0xfb exercises the '-'/'_' alphabet). */
export function vapidKey(byte: number): string {
  return btoa(String.fromCharCode(...new Array(65).fill(byte)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

export interface FakeSub {
  endpoint: string;
  options: { applicationServerKey: ArrayBuffer; userVisibleOnly: boolean };
  toJSON: () => PushSubscriptionJSON;
  unsubscribe: () => Promise<boolean>;
}

export function installPushEnv(
  opts: {
    permission?: NotificationPermission;
    grant?: NotificationPermission;
    subKeyByte?: number;
    registered?: boolean;
  } = {},
) {
  const state: { sub: FakeSub | null } = { sub: null };
  const makeSub = (key: ArrayBuffer): FakeSub => {
    const sub: FakeSub = {
      endpoint: ENDPOINT,
      options: { applicationServerKey: key, userVisibleOnly: true },
      toJSON: () => ({ endpoint: ENDPOINT, expirationTime: null, keys: { p256dh: 'p256', auth: 'auth' } }),
      unsubscribe: vi.fn(async () => {
        if (state.sub === sub) state.sub = null;
        return true;
      }),
    };
    return sub;
  };
  if (opts.subKeyByte !== undefined) state.sub = makeSub(new Uint8Array(65).fill(opts.subKeyByte).buffer);
  const pushManager = {
    getSubscription: vi.fn(async () => state.sub),
    subscribe: vi.fn(async (o: { applicationServerKey: Uint8Array; userVisibleOnly: boolean }) => {
      state.sub = makeSub(o.applicationServerKey.slice().buffer);
      return state.sub;
    }),
  };
  const reg = { pushManager };
  const serviceWorker = {
    register: vi.fn(async () => reg),
    ready: Promise.resolve(reg),
    getRegistration: vi.fn(async () => (opts.registered === false ? undefined : reg)),
  };
  Object.defineProperty(navigator, 'serviceWorker', { value: serviceWorker, configurable: true });
  const notification: { permission: NotificationPermission; requestPermission: () => Promise<NotificationPermission> } = {
    permission: opts.permission ?? 'default',
    requestPermission: vi.fn(async () => {
      notification.permission = opts.grant ?? 'granted';
      return notification.permission;
    }),
  };
  vi.stubGlobal('Notification', notification);
  vi.stubGlobal('PushManager', function PushManager() {});
  return { state, pushManager, serviceWorker, notification };
}

export function uninstallPushEnv() {
  delete (navigator as { serviceWorker?: unknown }).serviceWorker;
  vi.unstubAllGlobals();
}

/** fetch stub answering each call with `respond(method)`; records method + parsed body. */
export function stubFetch(respond: (method: string, init: RequestInit) => Promise<Response> | Response) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init = {}) => {
    const method = (init.method ?? 'GET').toUpperCase();
    calls.push({ url: String(url), method, body: init.body ? JSON.parse(String(init.body)) : undefined });
    return respond(method, init);
  });
  return { calls, spy };
}

/** A fetch that never answers until aborted, like a real hung request. */
export function hangUntilAborted(init: RequestInit): Promise<Response> {
  return new Promise((_, reject) => {
    init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  });
}
