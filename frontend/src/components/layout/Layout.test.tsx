import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Layout } from './Layout';
import { useConfigStore } from '@/store/config';
import { useInvitesStore } from '@/store/invites';
import type { AppConfig } from '@/services/api';
import { syncPushSubscription } from '@/services/push';

vi.mock('@/hooks/useSyncSocket', () => ({ useSyncSocket: () => {} }));
vi.mock('./ShellLayout', () => ({ ShellLayout: () => null }));
vi.mock('@/services/push', () => ({ syncPushSubscription: vi.fn(async () => {}) }));

const setKey = (key: string | null) =>
  useConfigStore.setState({ config: { push_public_key: key } as AppConfig });

describe('Layout push self-heal', () => {
  beforeEach(() => {
    vi.mocked(syncPushSubscription).mockClear();
    useConfigStore.setState({ config: null });
    useInvitesStore.setState({ fetchInvites: vi.fn(async () => {}) });
  });

  const renderLayout = () => render(<MemoryRouter><Layout /></MemoryRouter>);

  it('syncs once the key is known, and not again on a config refresh with the same key', () => {
    renderLayout();
    expect(syncPushSubscription).not.toHaveBeenCalled();
    act(() => setKey('KEY'));
    act(() => setKey('KEY'));
    expect(syncPushSubscription).toHaveBeenCalledTimes(1);
    expect(syncPushSubscription).toHaveBeenCalledWith('KEY');
  });

  it('never syncs when push is off', () => {
    setKey(null);
    renderLayout();
    expect(syncPushSubscription).not.toHaveBeenCalled();
  });

  it('swallows a sync failure', async () => {
    vi.mocked(syncPushSubscription).mockRejectedValueOnce(new Error('offline'));
    setKey('KEY');
    renderLayout();
    await act(async () => {});
    expect(syncPushSubscription).toHaveBeenCalledTimes(1);
  });
});
