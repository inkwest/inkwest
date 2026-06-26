import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the API module so the ticket POST is controllable; keep wsBaseUrl stable.
vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  wsBaseUrl: () => 'ws://test',
}));

import { fetchWsTicket } from '@/services/api';
import { ChatWebSocket } from './websocket';

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

  it("emits 'reconnecting' state:true when a dropped socket schedules a retry", async () => {
    vi.useFakeTimers();
    mockTicket.mockResolvedValue('T');
    const ws = new ChatWebSocket('s1');
    const events: Array<{ type: string; state?: boolean }> = [];
    ws.subscribe((e) => events.push(e as { type: string; state?: boolean }));

    await ws.connect();
    const sock = FakeWS.instances[0];
    sock.onopen?.();
    sock.onclose?.({ code: 1006 }); // abnormal close → backoff retry

    expect(events).toContainEqual({ type: 'reconnecting', state: true });
  });

  it("emits 'reconnecting' state:false on a clean close (no retry pending)", async () => {
    mockTicket.mockResolvedValue('T');
    const ws = new ChatWebSocket('s1');
    const events: Array<{ type: string; state?: boolean }> = [];
    ws.subscribe((e) => events.push(e as { type: string; state?: boolean }));

    await ws.connect();
    FakeWS.instances[0].onclose?.({ code: 1000 });

    expect(events).toContainEqual({ type: 'reconnecting', state: false });
    expect(events).not.toContainEqual({ type: 'reconnecting', state: true });
  });

  it("emits 'reconnecting' state:false on a terminal close code (no retry)", async () => {
    mockTicket.mockResolvedValue('T');
    const ws = new ChatWebSocket('s1');
    const events: Array<{ type: string; state?: boolean }> = [];
    ws.subscribe((e) => events.push(e as { type: string; state?: boolean }));

    await ws.connect();
    FakeWS.instances[0].onclose?.({ code: 4011 }); // share revoked — do not loop

    expect(events).toContainEqual({ type: 'reconnecting', state: false });
    expect(events).not.toContainEqual({ type: 'reconnecting', state: true });
  });

  it("emits a final 'reconnecting' state:false when ticket retries are exhausted", async () => {
    // Regression guard: the ticket POST failing through every retry (expired
    // session, auth-gateway outage spanning the backoff window) must end in a
    // give-up, not a stuck "Reconnecting…" with no attempt actually pending.
    vi.useFakeTimers();
    mockTicket.mockRejectedValue(new Error('401'));
    const ws = new ChatWebSocket('s1');
    const events: Array<{ type: string; state?: boolean }> = [];
    ws.subscribe((e) => events.push(e as { type: string; state?: boolean }));

    await ws.connect(); // attempt #1 fails → schedules retry
    // Drive every backoff timer (1+2+4+8+16s) through to the give-up.
    await vi.advanceTimersByTimeAsync(60000);

    expect(FakeWS.instances).toHaveLength(0); // never got past the ticket
    const reconnecting = events.filter((e) => e.type === 'reconnecting');
    expect(reconnecting.at(-1)).toEqual({ type: 'reconnecting', state: false });
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
