import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsModal } from './SettingsModal';
import { usePreferencesStore } from '@/store/preferences';
import { useSettingsStore } from '@/store/settings';
import { useConfigStore } from '@/store/config';
import * as api from '@/services/api';
import type { AppConfig } from '@/services/api';
import { bumpPushPermission } from '@/hooks/usePushOptIn';
import type { FakeSub } from '@/test/pushEnv';
import { ENDPOINT, installPushEnv, stubFetch, uninstallPushEnv, vapidKey } from '@/test/pushEnv';

describe('SettingsModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    vi.spyOn(api, 'getMyPreferences').mockResolvedValue({
      schema_version: 1,
      document: { ui: {} },
      skills: [],
    });
    vi.spyOn(api, 'updateMyPreferences').mockResolvedValue({
      schema_version: 1,
      document: {},
      skills: [],
    });
    usePreferencesStore.setState({ loaded: true, skills: [], disabledAdmin: [], document: {} });
    useSettingsStore.setState({
      compactToolCards: true,
      showTimestamps: true,
      showTokenCounts: true,
      wideComposer: false,
      sidebarWidth: 256,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('persists the token-counts toggle through saveUi', async () => {
    render(<SettingsModal isOpen onClose={() => {}} />);
    await userEvent.click(screen.getByText('Show token counts'));
    expect(useSettingsStore.getState().showTokenCounts).toBe(false);
    await waitFor(() => {
      const put = vi.mocked(api.updateMyPreferences);
      expect(put).toHaveBeenCalled();
      expect(put.mock.calls[0][0].ui?.showTokenCounts).toBe(false);
    });
  });

  it('renders the three tabs', async () => {
    render(<SettingsModal isOpen onClose={() => {}} />);
    expect(screen.getByRole('button', { name: 'Display' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Skills' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import / Export' })).toBeInTheDocument();
  });

  it('toggling a display setting persists via saveUi', async () => {
    const user = userEvent.setup();
    render(<SettingsModal isOpen onClose={() => {}} />);
    await user.click(screen.getByText('Wide composer'));
    await waitFor(() => expect(api.updateMyPreferences).toHaveBeenCalled());
    expect(useSettingsStore.getState().wideComposer).toBe(true);
  });

  it('shows the export control on the Import / Export tab', async () => {
    const user = userEvent.setup();
    render(<SettingsModal isOpen onClose={() => {}} />);
    await user.click(screen.getByText('Import / Export'));
    expect(screen.getByText('Export config')).toBeInTheDocument();
  });

  it('does not render when closed', () => {
    render(<SettingsModal isOpen={false} onClose={() => {}} />);
    expect(screen.queryByText('Display')).not.toBeInTheDocument();
  });

  it('gates the body until preferences finish loading', () => {
    // load() stubbed so `loaded` stays false → no writer (toggle) is reachable, which would
    // otherwise PUT a document missing skills.disabled_admin and drop the user's choices.
    usePreferencesStore.setState({ loaded: false, load: vi.fn() });
    render(<SettingsModal isOpen onClose={() => {}} />);
    expect(screen.getByText('Display')).toBeInTheDocument(); // tabs still show
    expect(screen.queryByText('Wide composer')).not.toBeInTheDocument(); // body gated
  });

  describe('Notifications tab', () => {
    const KEY = vapidKey(0xfb);
    const TOGGLE = { name: 'Browser notifications on this device' };
    const setKey = (key: string | null) =>
      useConfigStore.setState({ config: { push_public_key: key } as AppConfig });
    const openOnTab = () => render(<SettingsModal isOpen onClose={() => {}} initialTab="notifications" />);
    const ok = () => new Response(null, { status: 204 });

    beforeEach(() => setKey(KEY));
    afterEach(() => {
      uninstallPushEnv();
      useConfigStore.setState({ config: null });
    });

    it('is hidden when push is off, and a deep-link to it falls back to Display (003-AC13)', () => {
      setKey(null);
      openOnTab();
      expect(screen.queryByRole('button', { name: 'Notifications' })).not.toBeInTheDocument();
      expect(screen.getByText('Wide composer')).toBeInTheDocument();
    });

    it('is hidden before the config has loaded (003-AC13)', () => {
      useConfigStore.setState({ config: null });
      render(<SettingsModal isOpen onClose={() => {}} />);
      expect(screen.queryByRole('button', { name: 'Notifications' })).not.toBeInTheDocument();
    });

    it('ignores clicks while the subscription state is still loading (003-AC13)', async () => {
      const env = installPushEnv();
      env.pushManager.getSubscription.mockImplementation(() => new Promise(() => {}));
      const { calls } = stubFetch(() => ok());
      const user = userEvent.setup();
      openOnTab();
      const toggle = await screen.findByRole('button', TOGGLE);
      expect(toggle).toHaveAttribute('aria-disabled', 'true');
      await act(async () => {
        await user.click(toggle);
      });
      expect(env.notification.requestPermission).not.toHaveBeenCalled();
      expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(0);
    });

    it('never asks for permission on mount (003-AC13)', async () => {
      const env = installPushEnv();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'false');
      expect(env.notification.requestPermission).not.toHaveBeenCalled();
      expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    });

    it('turning on asks permission on the click and PUTs the subscription (003-AC13)', async () => {
      const env = installPushEnv();
      const { calls } = stubFetch(ok);
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true'));
      expect(env.notification.requestPermission).toHaveBeenCalledOnce();
      expect(calls.map((c) => [c.method, c.body])).toEqual([
        ['PUT', { endpoint: ENDPOINT, expirationTime: null, keys: { p256dh: 'p256', auth: 'auth' } }],
      ]);
    });

    it('turning off DELETEs the endpoint and unsubscribes (003-AC13)', async () => {
      const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
      const sub = env.state.sub!;
      const { calls } = stubFetch(ok);
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'false'));
      expect(calls.map((c) => [c.method, c.body])).toEqual([['DELETE', { endpoint: ENDPOINT }]]);
      expect(sub.unsubscribe).toHaveBeenCalledOnce();
    });

    it('shows the server error and stays off when the PUT fails (003-AC13)', async () => {
      installPushEnv();
      stubFetch(() => new Response(JSON.stringify({ detail: 'Push notifications are disabled' }), { status: 404 }));
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      expect(await screen.findByRole('alert')).toHaveTextContent('Push notifications are disabled');
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'false');
    });

    it('never renders a 422 body, which echoes the endpoint (003-AC13)', async () => {
      installPushEnv();
      const detail = [{ type: 'value_error', loc: ['body', 'endpoint'], msg: 'bad', input: ENDPOINT }];
      stubFetch(() => new Response(JSON.stringify({ detail }), { status: 422 }));
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent("This browser's push service isn't supported by Inkwest.");
      expect(document.body).not.toHaveTextContent(ENDPOINT);
    });

    it('announces the pending state while the request runs (003-AC13)', async () => {
      installPushEnv();
      let answer: (r: Response) => void = () => {};
      const { calls } = stubFetch(() => new Promise<Response>((r) => (answer = r)));
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Updating…'));
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'true');
      // Still focusable while busy, so a second click must be ignored, not start a second request.
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
      answer(ok());
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(''));
    });

    it('a bump from another surface mid-toggle does not unlock it (003-AC13)', async () => {
      const env = installPushEnv();
      let answer: (r: Response) => void = () => {};
      const { calls } = stubFetch(() => new Promise<Response>((r) => (answer = r)));
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1));
      // What the banner's Permissions `change` listener does when the prompt is answered.
      await act(async () => bumpPushPermission());
      expect(env.notification.permission).toBe('granted');
      expect(screen.getByRole('status')).toHaveTextContent('Updating…');
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'true');
      // Not "on" before the server has the subscription.
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'false');
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      expect(calls.filter((c) => c.method !== 'PUT')).toHaveLength(0);
      answer(ok());
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true'));
      expect(screen.getByRole('status')).toHaveTextContent('');
    });

    it('a late answer from an older re-read never overwrites a newer one (003-AC13)', async () => {
      const env = installPushEnv({ permission: 'granted', subKeyByte: 0xfb });
      let lateAnswer: (s: null) => void = () => {};
      // Mount read hangs and later answers "not subscribed"; the re-read after the bump answers first.
      env.pushManager.getSubscription.mockImplementationOnce(() => new Promise((r) => (lateAnswer = r)));
      openOnTab();
      await act(async () => bumpPushPermission());
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true'));
      await act(async () => lateAnswer(null));
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true');
    });

    it('a re-read started before the click cannot show "on" before the PUT answers (003-AC13)', async () => {
      // Granted but turned off: the case where a stray "subscribed" would flip the toggle on.
      const env = installPushEnv({ permission: 'granted' });
      let answer: (r: Response) => void = () => {};
      const { calls } = stubFetch(() => new Promise<Response>((r) => (answer = r)));
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      // A re-read from some bump that hangs until the click is under way, then answers "subscribed".
      let lateAnswer: (s: FakeSub | null) => void = () => {};
      env.pushManager.getSubscription.mockImplementationOnce(() => new Promise((r) => (lateAnswer = r)));
      await act(async () => bumpPushPermission());
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1));
      await act(async () => lateAnswer(env.state.sub));
      expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'false');
      answer(ok());
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-pressed', 'true'));
    });

    it('shows the blocked message when the prompt is answered "Block" (003-AC13)', async () => {
      installPushEnv({ grant: 'denied' });
      const user = userEvent.setup();
      openOnTab();
      await waitFor(() => expect(screen.getByRole('button', TOGGLE)).toHaveAttribute('aria-disabled', 'false'));
      await act(async () => {
        await user.click(screen.getByRole('button', TOGGLE));
      });
      expect(await screen.findByText(/Notifications are blocked for this site/)).toBeInTheDocument();
    });

    it('shows the blocked message when permission is already denied (003-AC13)', () => {
      installPushEnv({ permission: 'denied' });
      openOnTab();
      expect(screen.getByText(/Notifications are blocked for this site/)).toBeInTheDocument();
      expect(screen.queryByRole('button', TOGGLE)).not.toBeInTheDocument();
    });

    it('shows the unsupported message without PushManager (003-AC13)', () => {
      installPushEnv();
      delete (window as { PushManager?: unknown }).PushManager;
      openOnTab();
      expect(screen.getByText(/does not support push notifications/)).toBeInTheDocument();
      expect(screen.queryByRole('button', TOGGLE)).not.toBeInTheDocument();
    });
  });
});
