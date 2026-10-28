import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { InvitePopup } from './InvitePopup';
import { PushOptInBanner } from '@/components/layout/PushOptInBanner';
import { SettingsModal } from '@/components/settings/SettingsModal';
import { usePreferencesStore } from '@/store/preferences';
import * as api from '@/services/api';
import { PUSH_OPT_IN_SNOOZE_KEY } from '@/hooks/usePushOptIn';
import { useConfigStore } from '@/store/config';
import { useInvitesStore } from '@/store/invites';
import type { AppConfig } from '@/services/api';
import { ENDPOINT, installPushEnv, stubFetch, uninstallPushEnv, vapidKey } from '@/test/pushEnv';

const KEY = vapidKey(0xfb);
const OFFER = { name: 'Notify me on this browser next time' };
const setKey = (key: string | null) =>
  useConfigStore.setState({ config: { push_public_key: key } as AppConfig });
const renderPopup = () =>
  render(
    <MemoryRouter>
      <InvitePopup />
    </MemoryRouter>,
  );

describe('InvitePopup push offer', () => {
  beforeEach(() => {
    localStorage.clear();
    useInvitesStore.setState({
      invites: [
        {
          share_id: 'share-1',
          session_id: 'session-1',
          session_title: 'Incident',
          role: 'viewer',
          invited_by_display_name: 'Alice',
          created_at: '2026-10-28T00:00:00Z',
        },
      ],
    });
    setKey(KEY);
  });
  afterEach(() => {
    // Unmount before the fake browser APIs go away, not after.
    cleanup();
    uninstallPushEnv();
    vi.restoreAllMocks();
    useConfigStore.setState({ config: null });
    useInvitesStore.setState({ invites: [] });
    localStorage.clear();
  });

  it('offers push without prompting when the browser was never asked (003-AC16)', () => {
    const env = installPushEnv();
    renderPopup();
    expect(screen.getByRole('button', OFFER)).toBeInTheDocument();
    expect(env.notification.requestPermission).not.toHaveBeenCalled();
  });

  it.each([
    ['push is off', () => (setKey(null), installPushEnv())],
    ['permission already granted', () => installPushEnv({ permission: 'granted' })],
    ['permission denied', () => installPushEnv({ permission: 'denied' })],
    ['the browser has no push support', () => undefined],
  ])('hides the offer when %s (003-AC16)', (_label, setup) => {
    setup();
    renderPopup();
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument();
    expect(screen.queryByRole('button', OFFER)).not.toBeInTheDocument();
  });

  it('subscribes on click and confirms (003-AC16)', async () => {
    const env = installPushEnv();
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    renderPopup();
    // Mounted empty with the offer: screen readers announce the later change, not an insertion.
    expect(screen.getByRole('status')).toHaveTextContent('');
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Notifications are on for this browser.'),
    );
    expect(env.notification.requestPermission).toHaveBeenCalledTimes(1);
    const puts = calls.filter((c) => c.method === 'PUT');
    expect(puts).toHaveLength(1);
    expect(puts[0].url).toContain('/api/me/push-subscriptions');
    // The invite itself is untouched: Accept/Decline are still there.
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument();
  });

  it('explains a blocked prompt and sends nothing (003-AC16)', async () => {
    installPushEnv({ grant: 'denied' });
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    renderPopup();
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/blocked for this site/));
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(0);
    expect(screen.queryByRole('button', OFFER)).not.toBeInTheDocument();
  });

  it('shows a fixed error, never the 422 body, and keeps the offer to retry (003-AC16)', async () => {
    const env = installPushEnv();
    const body = JSON.stringify({ detail: [{ msg: 'bad host', input: ENDPOINT }] });
    stubFetch((method) =>
      method === 'PUT'
        ? new Response(body, { status: 422, headers: { 'Content-Type': 'application/json' } })
        : new Response(null, { status: 204 }),
    );
    const user = userEvent.setup();
    renderPopup();
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not turn on notifications.');
    expect(document.body).not.toHaveTextContent(ENDPOINT);
    // enablePush rolls the browser subscription back when the server refuses it.
    await waitFor(() => expect(env.state.sub).toBeNull());
    expect(screen.getByRole('button', OFFER)).toBeInTheDocument();
  });

  it('a prompt closed unanswered is not an error: the offer stays, no alert (003-AC16)', async () => {
    installPushEnv({ grant: 'default' });
    const { calls } = stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    renderPopup();
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() => expect(screen.getByRole('button', OFFER)).toBeEnabled());
    expect(screen.getByRole('status')).toHaveTextContent('No choice was made in the browser prompt.');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(0);
  });

  it('opting in from the popup also clears the top banner (003-AC16, 003-AC17)', async () => {
    installPushEnv();
    stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <PushOptInBanner />
        <InvitePopup />
      </MemoryRouter>,
    );
    expect(screen.getByRole('region', { name: 'Browser notifications' })).toBeInTheDocument();
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Browser notifications' })).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Notifications are on for this browser.'),
    );
  });

  it('a prompt closed unanswered from the popup also snoozes the banner (003-AC16, 003-AC17)', async () => {
    installPushEnv({ grant: 'default' });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <PushOptInBanner />
        <InvitePopup />
      </MemoryRouter>,
    );
    expect(screen.getByRole('region', { name: 'Browser notifications' })).toBeInTheDocument();
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Browser notifications' })).not.toBeInTheDocument(),
    );
    expect(Number(localStorage.getItem(PUSH_OPT_IN_SNOOZE_KEY))).toBeGreaterThan(Date.now());
  });

  it('opting in from the popup updates an open Settings toggle (003-AC16)', async () => {
    installPushEnv();
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    usePreferencesStore.setState({ loaded: true, skills: [], disabledAdmin: [], document: {} });
    stubFetch(() => new Response(null, { status: 204 }));
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <SettingsModal isOpen onClose={() => {}} initialTab="notifications" />
        <InvitePopup />
      </MemoryRouter>,
    );
    const toggle = screen.getByRole('button', { name: 'Browser notifications on this device' });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'false'));
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await act(async () => {
      await user.click(screen.getByRole('button', OFFER));
    });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'));
  });
});
