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
