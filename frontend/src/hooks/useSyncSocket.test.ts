import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';

const navigateMock = vi.fn();
vi.mock('react-router-dom', () => ({ useNavigate: () => navigateMock }));
vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  getVersion: vi.fn().mockResolvedValue({ version: 'v1', build_timestamp: 't' }),
  wsBaseUrl: () => 'ws://test',
}));

import { fetchWsTicket } from '@/services/api';
import { useSyncSocket } from './useSyncSocket';
import { useBackendStore } from '@/store/backend';

const mockTicket = fetchWsTicket as unknown as ReturnType<typeof vi.fn>;

class FakeWS {
  static OPEN = 1;
  static instances: FakeWS[] = [];
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  close = vi.fn();
  send = vi.fn();
  constructor(
    public url: string,
    public protocols?: string | string[],
  ) {
    FakeWS.instances.push(this);
  }
}

describe('useSyncSocket — reconnect & give-up', () => {
  beforeEach(() => {
    FakeWS.instances = [];
    mockTicket.mockReset();
    navigateMock.mockReset();
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('flags gaveUp (red reload banner) once the 10-min budget is spent — it is the heartbeat gate', async () => {
    vi.useFakeTimers();
    mockTicket.mockRejectedValue(new Error('ticket down'));
    const { unmount } = renderHook(() => useSyncSocket());

    // Drive reconnect attempts across >10 min of virtual time (fake timers also
    // advance Date.now()); each ticket fetch rejects so no socket ever opens.
    for (let i = 0; i < 40; i++) {
      await vi.advanceTimersByTimeAsync(30000);
    }

    expect(useBackendStore.getState().gaveUp).toBe(true);
    unmount();
  });

  it('does NOT reconnect on a clean (1000) close', async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0); // flush the awaited ticket fetch
    expect(FakeWS.instances).toHaveLength(1);

    FakeWS.instances[0].onclose?.({ code: 1000 });
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeWS.instances).toHaveLength(1); // no reconnect
    unmount();
  });

  it('clears the backend banner on open', async () => {
    vi.useFakeTimers();
    useBackendStore.setState({ unavailable: true, retrying: 1 });
    mockTicket.mockResolvedValue('T');
    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0);

    FakeWS.instances[0].readyState = FakeWS.OPEN;
    FakeWS.instances[0].onopen?.();
    expect(useBackendStore.getState().unavailable).toBe(false);
    unmount();
  });
});
