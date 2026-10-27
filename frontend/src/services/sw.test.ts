import { describe, it, expect, vi } from 'vitest';
import swSource from '../../public/sw.js?raw';

type Handler = (event: unknown) => void;

// Evaluates public/sw.js against a stubbed worker global: no browser, just its event handlers.
function loadWorker(windows: { focused: boolean; focus?: () => Promise<unknown> }[]) {
  const handlers: Record<string, Handler> = {};
  const self = {
    addEventListener: (type: string, fn: Handler) => (handlers[type] = fn),
    clients: {
      matchAll: vi.fn(async () => windows),
      openWindow: vi.fn(async () => null),
    },
    registration: { showNotification: vi.fn(async () => undefined) },
    skipWaiting: vi.fn(async () => undefined),
  };
  new Function('self', swSource)(self);
  const fire = async (type: string, event: object) => {
    const pending: Promise<unknown>[] = [];
    handlers[type]({ ...event, waitUntil: (p: Promise<unknown>) => pending.push(p) });
    await Promise.all(pending);
  };
  const push = (data: { json: () => unknown } | null) => fire('push', { data });
  return { self, push, fire };
}

const INVITE = {
  type: 'share_invite',
  share_id: 'share-1',
  session_id: 'sess-1',
  session_title: 'Prod outage',
  invited_by: 'Alice',
};
const payload = (v: unknown) => ({ json: () => v });
// Uncontrolled windows count: sw.js never claims clients, so the tab that enabled push isn't controlled.
const ALL_WINDOWS = { type: 'window', includeUncontrolled: true };

describe('service worker install', () => {
  it('activates a new version without waiting for tabs to close', async () => {
    const w = loadWorker([]);
    await w.fire('install', {});
    expect(w.self.skipWaiting).toHaveBeenCalled();
  });
});

describe('service worker push', () => {
  it('shows nothing when an Inkwest window is focused (003-AC11)', async () => {
    const w = loadWorker([{ focused: false }, { focused: true }]);
    await w.push(payload(INVITE));
    expect(w.self.clients.matchAll).toHaveBeenCalledWith(ALL_WINDOWS);
    expect(w.self.registration.showNotification).not.toHaveBeenCalled();
  });

  it('notifies with tag = share id when no window is focused (003-AC11)', async () => {
    const w = loadWorker([{ focused: false }]);
    await w.push(payload(INVITE));
    expect(w.self.registration.showNotification).toHaveBeenCalledWith(
      'Alice shared “Prod outage” with you',
      expect.objectContaining({ tag: 'share-1', data: { session_id: 'sess-1' } }),
    );
  });

  it('falls back when the title and inviter name are null', async () => {
    const w = loadWorker([]);
    await w.push(payload({ ...INVITE, session_title: null, invited_by: null }));
    expect(w.self.registration.showNotification).toHaveBeenCalledWith(
      'Someone shared a conversation with you',
      expect.anything(),
    );
  });

  it.each([
    ['no data', null],
    ['invalid JSON', { json: () => JSON.parse('{nope') }],
    ['null JSON', payload(null)],
    ['another event type', payload({ ...INVITE, type: 'share_revoked' })],
  ])('shows nothing and does not throw on %s', async (_label, data) => {
    const w = loadWorker([]);
    await expect(w.push(data)).resolves.toBeUndefined();
    expect(w.self.registration.showNotification).not.toHaveBeenCalled();
  });
});

describe('service worker notificationclick', () => {
  const click = () => ({ notification: { close: vi.fn() } });

  it('closes the notification and focuses an existing window without navigating it', async () => {
    const focus = vi.fn(async () => undefined);
    const w = loadWorker([{ focused: false, focus }]);
    const event = click();
    await w.fire('notificationclick', event);
    expect(event.notification.close).toHaveBeenCalled();
    expect(w.self.clients.matchAll).toHaveBeenCalledWith(ALL_WINDOWS);
    expect(focus).toHaveBeenCalled();
    expect(w.self.clients.openWindow).not.toHaveBeenCalled();
  });

  it('opens / when no Inkwest window exists', async () => {
    const w = loadWorker([]);
    await w.fire('notificationclick', click());
    expect(w.self.clients.openWindow).toHaveBeenCalledWith('/');
  });
});
