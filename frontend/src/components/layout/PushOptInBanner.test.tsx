import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PushOptInBanner } from './PushOptInBanner';
import { PUSH_OPT_IN_SNOOZE_KEY, PUSH_OPT_IN_SNOOZE_MS } from '@/hooks/usePushOptIn';
import { SettingsModal } from '@/components/settings/SettingsModal';
import { useConfigStore } from '@/store/config';
import { usePreferencesStore } from '@/store/preferences';
import * as api from '@/services/api';
import type { AppConfig } from '@/services/api';
import { ENDPOINT, installPushEnv, stubFetch, uninstallPushEnv, vapidKey } from '@/test/pushEnv';

const KEY = vapidKey(0xfb);
const BANNER = { name: 'Browser notifications' };
const setKey = (key: string | null) =>
  useConfigStore.setState({ config: { push_public_key: key } as AppConfig });

describe('PushOptInBanner', () => {
  beforeEach(() => {
    localStorage.clear();
    setKey(KEY);
  });
  afterEach(() => {
    // Unmount before the fake browser APIs go away, not after.
    cleanup();
    uninstallPushEnv();
    delete (navigator as { permissions?: unknown }).permissions;
    vi.restoreAllMocks();
    vi.useRealTimers();
    localStorage.clear();
    useConfigStore.setState({ config: null });
  });

  it('asks in-app without raising the browser prompt (003-AC17)', () => {
    const env = installPushEnv();
    render(<PushOptInBanner />);
    expect(screen.getByRole('region', BANNER)).toBeInTheDocument();
    expect(env.notification.requestPermission).not.toHaveBeenCalled();
  });

  it.each([
    ['push is off', () => (setKey(null), installPushEnv())],
    ['permission already granted', () => installPushEnv({ permission: 'granted' })],
    ['permission denied', () => installPushEnv({ permission: 'denied' })],
    ['the browser has no push support', () => undefined],
  ])('stays hidden when %s (003-AC17)', (_label, setup) => {
    setup();
    render(<PushOptInBanner />);
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
  });

  it('"Turn on" raises the prompt, subscribes, then disappears (003-AC17)', async () => {
    const env = installPushEnv();
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Turn on' }));
    });
    await waitFor(() => expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument());
    expect(env.notification.requestPermission).toHaveBeenCalledTimes(1);
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
  });

  it('a prompt closed unanswered counts as "Not now": hidden and snoozed (003-AC17)', async () => {
    installPushEnv({ grant: 'default' });
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Turn on' }));
    });
    await waitFor(() => expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument());
    expect(Number(localStorage.getItem(PUSH_OPT_IN_SNOOZE_KEY))).toBeGreaterThan(Date.now());
  });

  it('a double click raises the browser prompt once (003-AC17)', async () => {
    const env = installPushEnv();
    let answer: (p: NotificationPermission) => void = () => {};
    vi.mocked(env.notification.requestPermission).mockImplementation(
      () => new Promise<NotificationPermission>((r) => (answer = r)),
    );
    stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.dblClick(screen.getByRole('button', { name: 'Turn on' }));
    });
    expect(env.notification.requestPermission).toHaveBeenCalledTimes(1);
    env.notification.permission = 'granted';
    answer('granted');
    await waitFor(() => expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument());
  });

  it('turning push on from Settings clears the banner (003-AC17)', async () => {
    installPushEnv();
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    usePreferencesStore.setState({ loaded: true, skills: [], disabledAdmin: [], document: {} });
    stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(
      <>
        <PushOptInBanner />
        <SettingsModal isOpen onClose={() => {}} initialTab="notifications" />
      </>,
    );
    expect(screen.getByRole('region', BANNER)).toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: 'Browser notifications on this device' });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'false'));
    await act(async () => {
      await user.click(toggle);
    });
    await waitFor(() => expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument());
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'));
  });

  it('follows a permission change made outside the page (003-AC17)', async () => {
    const env = installPushEnv();
    // Real PermissionStatus shape: an EventTarget with `state`, firing `change`.
    const status = Object.assign(new EventTarget(), { state: 'prompt', name: 'notifications' });
    Object.defineProperty(navigator, 'permissions', {
      value: { query: vi.fn(async () => status) },
      configurable: true,
    });
    render(<PushOptInBanner />);
    expect(screen.getByRole('region', BANNER)).toBeInTheDocument();
    await waitFor(() => expect(navigator.permissions.query).toHaveBeenCalled());
    await act(async () => {
      env.notification.permission = 'granted';
      status.state = 'granted';
      status.dispatchEvent(new Event('change'));
    });
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
  });

  it('disappears when the browser prompt is blocked (003-AC17)', async () => {
    installPushEnv({ grant: 'denied' });
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Turn on' }));
    });
    await waitFor(() => expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument());
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(0);
  });

  it('says so when the server refuses the subscription, never echoing the 422 body (003-AC17)', async () => {
    installPushEnv();
    const body = JSON.stringify({ detail: [{ msg: 'bad host', input: ENDPOINT }] });
    stubFetch((method) =>
      method === 'PUT'
        ? new Response(body, { status: 422, headers: { 'Content-Type': 'application/json' } })
        : new Response(null, { status: 204 }),
    );
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Turn on' }));
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not turn on notifications.');
    expect(document.body).not.toHaveTextContent(ENDPOINT);
  });

  it.each([['Not now'], ['Dismiss']])('"%s" hides it for 30 days on this browser (003-AC17)', async (name) => {
    installPushEnv();
    const t0 = new Date('2026-10-28T10:00:00Z').getTime();
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(t0);
    const user = userEvent.setup();
    const first = render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name }));
    });
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
    expect(Number(localStorage.getItem(PUSH_OPT_IN_SNOOZE_KEY))).toBe(t0 + PUSH_OPT_IN_SNOOZE_MS);
    first.unmount();

    vi.setSystemTime(t0 + PUSH_OPT_IN_SNOOZE_MS - 1000);
    const second = render(<PushOptInBanner />);
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
    second.unmount();

    vi.setSystemTime(t0 + PUSH_OPT_IN_SNOOZE_MS + 1000);
    render(<PushOptInBanner />);
    expect(screen.getByRole('region', BANNER)).toBeInTheDocument();
  });

  it('still asks when localStorage is unavailable (003-AC17)', async () => {
    installPushEnv();
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const user = userEvent.setup();
    render(<PushOptInBanner />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Not now' }));
    });
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
  });

  it('detaches its permission listener on unmount, and never attaches one after (003-AC17)', async () => {
    installPushEnv();
    let resolveQuery: (s: EventTarget) => void = () => {};
    const status = Object.assign(new EventTarget(), { state: 'prompt', name: 'notifications' });
    const add = vi.spyOn(status, 'addEventListener');
    const remove = vi.spyOn(status, 'removeEventListener');
    Object.defineProperty(navigator, 'permissions', {
      value: { query: vi.fn(() => new Promise((r) => (resolveQuery = r))) },
      configurable: true,
    });
    // Unmounted before the query resolves: nothing may be attached.
    const early = render(<PushOptInBanner />);
    early.unmount();
    await act(async () => resolveQuery(status));
    expect(add).not.toHaveBeenCalled();
    // Unmounted after: the very handler it added is removed.
    const late = render(<PushOptInBanner />);
    await act(async () => resolveQuery(status));
    await waitFor(() => expect(add).toHaveBeenCalled());
    const handler = add.mock.calls[0][1];
    late.unmount();
    expect(remove).toHaveBeenCalledWith('change', handler);
  });

  it('still asks when the Permissions API rejects the query (003-AC17)', async () => {
    installPushEnv();
    const query = vi.fn(async () => {
      throw new TypeError('notifications is not a valid PermissionName');
    });
    Object.defineProperty(navigator, 'permissions', { value: { query }, configurable: true });
    render(<PushOptInBanner />);
    await waitFor(() => expect(query).toHaveBeenCalled());
    expect(screen.getByRole('region', BANNER)).toBeInTheDocument();
  });

  it('a success from Settings clears the banner\'s earlier "Could not turn on" (003-AC17)', async () => {
    installPushEnv();
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    usePreferencesStore.setState({ loaded: true, skills: [], disabledAdmin: [], document: {} });
    let puts = 0;
    stubFetch((method) =>
      method === 'PUT' && ++puts === 1
        ? new Response(JSON.stringify({ detail: 'boom' }), { status: 500 })
        : new Response(null, { status: 204 }),
    );
    const user = userEvent.setup();
    render(
      <>
        <PushOptInBanner />
        <SettingsModal isOpen onClose={() => {}} initialTab="notifications" />
      </>,
    );
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Turn on' }));
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not turn on notifications.');
    const toggle = screen.getByRole('button', { name: 'Browser notifications on this device' });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'false'));
    await act(async () => {
      await user.click(toggle);
    });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.queryByRole('region', BANNER)).not.toBeInTheDocument();
  });
});
