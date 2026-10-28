import { ApiError, deletePushSubscription, putPushSubscription } from '@/services/api';

// Logout waits at most this long on push cleanup before invalidating the session anyway.
export const LOGOUT_PUSH_TIMEOUT_MS = 2000;

export function isPushSupported(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

export function currentPermission(): NotificationPermission {
  return Notification.permission;
}

// `ready`, not the register() result: subscribe() rejects until the worker is active.
export async function getRegistration(): Promise<ServiceWorkerRegistration> {
  await navigator.serviceWorker.register('/sw.js');
  return navigator.serviceWorker.ready;
}

// Read-only: never registers the worker, so it is safe on logout and when the feature is off.
export async function getPushSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.getRegistration('/');
  return (await reg?.pushManager.getSubscription()) ?? null;
}

function b64urlToBytes(s: string) {
  return Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
}

function sameKey(a: ArrayBuffer | null, b: Uint8Array): boolean {
  const x = a ? new Uint8Array(a) : null;
  return !!x && x.length === b.length && x.every((v, i) => v === b[i]);
}

// subscribe() with a different VAPID key throws InvalidStateError, so drop a stale one first.
async function subscribe(reg: ServiceWorkerRegistration, publicKey: string): Promise<PushSubscription> {
  const key = b64urlToBytes(publicKey);
  const existing = await reg.pushManager.getSubscription();
  if (existing && sameKey(existing.options.applicationServerKey, key)) return existing;
  if (existing) {
    // Best effort: a row left behind holds a cap slot until a send to it 410s.
    await deletePushSubscription(existing.endpoint).catch(() => {});
    await existing.unsubscribe();
  }
  return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
}

/** Must be called from a user click: it may raise the browser permission prompt. */
export async function enablePush(publicKey: string): Promise<void> {
  if ((await Notification.requestPermission()) !== 'granted') {
    throw new Error('Notification permission was not granted.');
  }
  const sub = await subscribe(await getRegistration(), publicKey);
  try {
    await putPushSubscription(sub.toJSON());
  } catch (e) {
    // A browser subscription the server doesn't know would show the toggle on yet never deliver.
    await sub.unsubscribe().catch(() => {});
    throw e;
  }
}

export async function disablePush(signal?: AbortSignal): Promise<void> {
  const sub = await getPushSubscription();
  if (!sub) return;
  try {
    await deletePushSubscription(sub.endpoint, signal);
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) throw e;
  } finally {
    // Unsubscribe even when the DELETE failed: the dead endpoint 410s and the backend prunes the row.
    await sub.unsubscribe();
  }
}

/** Bounded, never throws: logout must proceed whatever the push service or backend does. */
export async function disablePushForLogout(): Promise<void> {
  if (!isPushSupported()) return;
  const ctrl = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<void>((resolve) => {
    timer = setTimeout(() => {
      ctrl.abort();
      resolve();
    }, LOGOUT_PUSH_TIMEOUT_MS);
  });
  try {
    await Promise.race([disablePush(ctrl.signal), deadline]);
  } catch {
    // Best effort.
  } finally {
    clearTimeout(timer);
  }
}

/** Self-heal on load: re-PUT a rotated endpoint, resubscribe on a VAPID key change. Never prompts. */
export async function syncPushSubscription(publicKey: string): Promise<void> {
  if (!isPushSupported() || currentPermission() !== 'granted') return;
  const reg = await getRegistration();
  if (!(await reg.pushManager.getSubscription())) return;
  await putPushSubscription((await subscribe(reg, publicKey)).toJSON());
}
