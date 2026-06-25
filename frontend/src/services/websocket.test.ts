import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the API module so the ticket POST is controllable; keep wsBaseUrl stable.
vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  wsBaseUrl: () => 'ws://test',
}));

import { fetchWsTicket } from '@/services/api';
import { ChatWebSocket } from './websocket';
import { useBackendStore } from '@/store/backend';

const mockTicket = fetchWsTicket as unknown as ReturnType<typeof vi.fn>;

// Protocol-capturing WebSocket fake (the global setup mock drops the protocols
// arg, which is exactly what these tests assert on).
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

describe('ChatWebSocket.connect — ticket auth', () => {
  beforeEach(() => {
    FakeWS.instances = [];
    mockTicket.mockReset();
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('opens the socket with the ticket subprotocol when the ticket POST succeeds', async () => {
    mockTicket.mockResolvedValue('TICKET123');
    const ws = new ChatWebSocket('s1');

    await ws.connect();

    expect(mockTicket).toHaveBeenCalledOnce();
    expect(FakeWS.instances).toHaveLength(1);
    expect(FakeWS.instances[0].protocols).toEqual(['inkwest', 'ticket|TICKET123']);
    expect(FakeWS.instances[0].url).toBe('ws://test/ws/chat/s1');
  });

  it('on ticket failure: emits an error, opens no socket, and schedules a reconnect', async () => {
    vi.useFakeTimers();
    mockTicket.mockRejectedValue(new Error('401'));
    const ws = new ChatWebSocket('s1');
    const events: Array<{ type: string; content?: string }> = [];
    ws.subscribe((e) => events.push(e as { type: string; content?: string }));

    await ws.connect();

    // No socket created on a failed ticket fetch.
    expect(FakeWS.instances).toHaveLength(0);
    expect(events).toContainEqual({ type: 'error', content: 'Failed to authenticate connection' });

    // A reconnect must be scheduled: advancing the timer re-enters connect(),
    // which fetches a fresh ticket (now succeeding) and opens the socket.
    mockTicket.mockResolvedValue('TICKET456');
    await vi.advanceTimersByTimeAsync(2000);

    expect(mockTicket).toHaveBeenCalledTimes(2);
    expect(FakeWS.instances).toHaveLength(1);
    expect(FakeWS.instances[0].protocols).toEqual(['inkwest', 'ticket|TICKET456']);
  });

  it('does not reconnect after disconnect() even if a ticket fetch was in flight', async () => {
    vi.useFakeTimers();
    mockTicket.mockRejectedValue(new Error('401'));
    const ws = new ChatWebSocket('s1');

    await ws.connect();
    ws.disconnect();

    mockTicket.mockResolvedValue('TICKET789');
    await vi.advanceTimersByTimeAsync(5000);

    // disconnect() cancels the pending reconnect timer — no further attempts.
    expect(mockTicket).toHaveBeenCalledOnce();
    expect(FakeWS.instances).toHaveLength(0);
  });
});

describe('ChatWebSocket — reconnect & teardown behavior', () => {
  beforeEach(() => {
    FakeWS.instances = [];
    mockTicket.mockReset();
    mockTicket.mockResolvedValue('TICKET');
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it.each([4004, 4010, 4011])('does NOT reconnect on terminal close code %i', async (code) => {
    vi.useFakeTimers();
    const ws = new ChatWebSocket('s1');
    await ws.connect();
    expect(FakeWS.instances).toHaveLength(1);

    FakeWS.instances[0].onclose?.({ code });
    await vi.advanceTimersByTimeAsync(60000);

    expect(FakeWS.instances).toHaveLength(1); // no new socket
  });

  it('does NOT reconnect on a clean close (1000)', async () => {
    vi.useFakeTimers();
    const ws = new ChatWebSocket('s1');
    await ws.connect();
    FakeWS.instances[0].onclose?.({ code: 1000 });
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeWS.instances).toHaveLength(1);
  });

  it('keeps reconnecting past the old 5-attempt cap on transient closes', async () => {
    vi.useFakeTimers();
    const ws = new ChatWebSocket('s1');
    await ws.connect();

    // Drive 7 transient (1006) closes — old code capped at 5.
    for (let i = 0; i < 7; i++) {
      FakeWS.instances[FakeWS.instances.length - 1].onclose?.({ code: 1006 });
      await vi.advanceTimersByTimeAsync(60000);
    }
    // 1 initial + 7 reconnects = 8 sockets (would be capped at 6 under the old cap).
    expect(FakeWS.instances.length).toBe(8);
  });

  it('stops reconnecting after the 10-min budget but does NOT flag gaveUp (chat gives up quietly)', async () => {
    vi.useFakeTimers();
    const ws = new ChatWebSocket('s1');
    await ws.connect();

    // Hammer transient closes across >10 min of virtual time (fake timers also
    // advance Date.now()), each at the max ~30s backoff.
    for (let i = 0; i < 40; i++) {
      FakeWS.instances[FakeWS.instances.length - 1].onclose?.({ code: 1006 });
      await vi.advanceTimersByTimeAsync(30000);
    }

    const count = FakeWS.instances.length;
    // No further reconnect after giving up.
    FakeWS.instances[FakeWS.instances.length - 1].onclose?.({ code: 1006 });
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeWS.instances.length).toBe(count);
    // Chat is per-session and remounts on navigation, so it must NOT raise the
    // global reload banner — that's the sync socket's job.
    expect(useBackendStore.getState().gaveUp).toBe(false);
  });

  it('clears the backend banner on open (proof the backend is reachable)', async () => {
    useBackendStore.setState({ unavailable: true, retrying: 1 });
    const ws = new ChatWebSocket('s1');
    await ws.connect();
    FakeWS.instances[0].readyState = FakeWS.OPEN;
    FakeWS.instances[0].onopen?.();
    expect(useBackendStore.getState().unavailable).toBe(false);
  });

  it('aborts the in-flight ticket fetch on disconnect()', async () => {
    let captured: AbortSignal | undefined;
    mockTicket.mockImplementation((signal?: AbortSignal) => {
      captured = signal;
      return new Promise(() => {}); // never resolves — stays in flight
    });
    const ws = new ChatWebSocket('s1');
    void ws.connect();
    await Promise.resolve(); // let connect() reach the awaited ticket fetch
    ws.disconnect();
    expect(captured?.aborted).toBe(true);
  });

  it('caps the offline queue at 50, rejecting the newest (no eviction of queued)', async () => {
    const ws = new ChatWebSocket('s1');
    for (let i = 0; i < 60; i++) ws.sendMessage(`m${i}`);
    await ws.connect();
    const sock = FakeWS.instances[0];
    sock.onopen?.(); // flush the queue

    const msgs = sock.send.mock.calls
      .map((c) => JSON.parse(c[0] as string))
      .filter((m) => m.type === 'message');
    expect(msgs).toHaveLength(50);
    expect(msgs[0].content).toBe('m0'); // oldest kept
    expect(msgs[49].content).toBe('m49'); // newest-at-cap rejected (m50..m59 dropped)
  });
});
