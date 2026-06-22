import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

vi.mock('@/services/api', () => ({
  fetchWsTicket: vi.fn(),
  wsBaseUrl: () => 'ws://test',
}));

import { fetchWsTicket } from '@/services/api';
import { useEgressSocket } from './useEgressSocket';

const mockTicket = fetchWsTicket as unknown as ReturnType<typeof vi.fn>;

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
