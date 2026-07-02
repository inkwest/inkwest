import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';

const navigateMock = vi.fn();
vi.mock('react-router-dom', () => ({ useNavigate: () => navigateMock }));
vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  getVersion: vi.fn().mockResolvedValue({ version: 'v1', build_timestamp: 't' }),
  wsBaseUrl: () => 'ws://test',
}));
vi.mock('@/lib/mcpDek', () => ({ dekReply: vi.fn() }));
vi.mock('@/components/connector/mcpCatalogCache', () => ({ resetMcpCatalogCache: vi.fn() }));

import { fetchWsTicket } from '@/services/api';
import { dekReply } from '@/lib/mcpDek';
import { useSyncSocket } from './useSyncSocket';
import { useBackendStore } from '@/store/backend';
import { useMcpStore } from '@/store/mcp';
import { useSessionsStore } from '@/store/sessions';
import { resetMcpCatalogCache } from '@/components/connector/mcpCatalogCache';

const mockResetCatalog = resetMcpCatalogCache as unknown as ReturnType<typeof vi.fn>;

const mockTicket = fetchWsTicket as unknown as ReturnType<typeof vi.fn>;
const mockDekReply = dekReply as unknown as ReturnType<typeof vi.fn>;

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
    mockDekReply.mockReset();
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

  it('kicks off the MCP DEK handshake on open (key seated without a chat socket)', async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0);

    const ws = FakeWS.instances[0];
    ws.readyState = FakeWS.OPEN;
    ws.onopen?.();

    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'mcp_dek_open' }));
    unmount();
  });

  it('computes dekReply for an mcp_dek_* frame and sends the reply back', async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    mockDekReply.mockResolvedValue({ type: 'mcp_dek_supply', dek: 'abc' });
    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0);

    const ws = FakeWS.instances[0];
    ws.readyState = FakeWS.OPEN;
    const frame = { type: 'mcp_dek_challenge', wrapped_dek: 'w', wrap_iv: 'iv' };
    (ws.onmessage as unknown as (e: { data: string }) => void)?.({ data: JSON.stringify(frame) });
    await vi.advanceTimersByTimeAsync(0); // flush the awaited dekReply promise

    expect(mockDekReply).toHaveBeenCalledWith(frame);
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'mcp_dek_supply', dek: 'abc' }));
    unmount();
  });

  it('on mcp_catalog_changed: resets the catalog cache, bumps the version, skips fetchSessions', async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    mockResetCatalog.mockClear();
    useMcpStore.setState({ catalogVersion: 0 });
    const fetchSessionsSpy = vi.fn();
    useSessionsStore.setState({ fetchSessions: fetchSessionsSpy });

    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0);

    const ws = FakeWS.instances[0];
    ws.readyState = FakeWS.OPEN;
    (ws.onmessage as unknown as (e: { data: string }) => void)?.({
      data: JSON.stringify({ type: 'mcp_catalog_changed' }),
    });

    expect(mockResetCatalog).toHaveBeenCalledTimes(1);
    expect(useMcpStore.getState().catalogVersion).toBe(1);
    // Not a session event — must return before the unconditional fetchSessions().
    expect(fetchSessionsSpy).not.toHaveBeenCalled();
    unmount();
  });

  it('swallows a dekReply rejection (crypto failure) without sending or throwing', async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    mockDekReply.mockRejectedValue(new Error('subtle crypto blew up'));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { unmount } = renderHook(() => useSyncSocket());
    await vi.advanceTimersByTimeAsync(0);

    const ws = FakeWS.instances[0];
    ws.readyState = FakeWS.OPEN;
    ws.send.mockClear(); // drop the mcp_dek_open from onopen
    (ws.onmessage as unknown as (e: { data: string }) => void)?.({
      data: JSON.stringify({ type: 'mcp_dek_challenge', wrapped_dek: 'w' }),
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(ws.send).not.toHaveBeenCalled();
    expect(errSpy).toHaveBeenCalled();
    errSpy.mockRestore();
    unmount();
  });
});
