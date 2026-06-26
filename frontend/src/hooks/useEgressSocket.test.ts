import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  wsBaseUrl: () => 'ws://test',
}));

import { fetchWsTicket } from '@/services/api';
import { useEgressSocket, isBlocked, isError, EgressEvent } from './useEgressSocket';

const mockTicket = fetchWsTicket as unknown as ReturnType<typeof vi.fn>;

function ev(partial: Partial<EgressEvent>): EgressEvent {
  return {
    _id: 0,
    ts: 0,
    decision: 'TCP_TUNNEL',
    status: '200',
    method: 'CONNECT',
    host: 'example.com',
    port: '443',
    bytes: 0,
    client: 'c',
    ...partial,
  };
}

describe('egress classification', () => {
  it('treats only a DENIED decision as a squid block', () => {
    expect(isBlocked(ev({ decision: 'TCP_DENIED', status: '403' }))).toBe(true);
    expect(isError(ev({ decision: 'TCP_DENIED', status: '403' }))).toBe(false);
  });

  it('treats a 5xx on a forwarded tunnel as an error, not a block', () => {
    // The screenshot case: squid established the tunnel (HIER_DIRECT) and the
    // upstream EKS endpoint returned 503 — unrelated to egress policy.
    const e = ev({ decision: 'TCP_TUNNEL', status: '503' });
    expect(isBlocked(e)).toBe(false);
    expect(isError(e)).toBe(true);
  });

  it('treats an upstream 4xx as an error, not a block', () => {
    const e = ev({ decision: 'TCP_MISS', status: '403' });
    expect(isBlocked(e)).toBe(false);
    expect(isError(e)).toBe(true);
  });

  it('treats a successful tunnel as neither blocked nor errored', () => {
    const e = ev({ decision: 'TCP_TUNNEL', status: '200' });
    expect(isBlocked(e)).toBe(false);
    expect(isError(e)).toBe(false);
  });
});

describe('useEgressSocket — ticket auth', () => {
  beforeEach(() => mockTicket.mockReset());

  // The ticket-failure → backoff-reconnect branch is covered deterministically
  // by services/websocket.test.ts (identical catch logic on the chat socket);
  // here we cover the egress-specific success wiring.
  it('opens the egress socket with the ticket subprotocol on success', async () => {
    mockTicket.mockResolvedValue('TICKET123');
    const seen: Array<{ url: string; protocols?: string | string[] }> = [];
    class FakeWS {
      onopen: (() => void) | null = null;
      onclose: (() => void) | null = null;
      onmessage: (() => void) | null = null;
      onerror: (() => void) | null = null;
      close = vi.fn();
      constructor(
        public url: string,
        public protocols?: string | string[],
      ) {
        seen.push({ url, protocols });
      }
    }
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);

    const { unmount } = renderHook(() => useEgressSocket('s1'));
    await waitFor(() => expect(seen).toHaveLength(1));

    expect(seen[0].url).toBe('ws://test/ws/egress/s1');
    expect(seen[0].protocols).toEqual(['inkwest', 'ticket|TICKET123']);

    unmount();
    vi.unstubAllGlobals();
  });
});
