import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

vi.mock('@/services/api', () => ({
  getSharedConversation: vi.fn(),
  wsBaseUrl: () => 'ws://test',
  // Mirror the real ApiError so the hook's `e instanceof ApiError` branch works.
  ApiError: class ApiError extends Error {
    constructor(
      public readonly status: number,
      message: string
    ) {
      super(message);
      this.name = 'ApiError';
    }
  },
}));

import { getSharedConversation, ApiError } from '@/services/api';
import { useSharedSocket } from './useSharedSocket';
import { useChatStore } from '@/store/chat';

const mockGet = getSharedConversation as unknown as ReturnType<typeof vi.fn>;

let sockets: FakeWS[] = [];

class FakeWS {
  static readonly OPEN = 1;
  readyState = FakeWS.OPEN;
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor(
    public url: string,
    public protocols?: string | string[]
  ) {
    sockets.push(this);
  }
}

const msg = (id: string) => ({
  id,
  role: 'user',
  content: id,
  created_at: '2026-01-01T00:00:00Z',
});

function snapshot(...ids: string[]) {
  return {
    session_id: 's1',
    title: 'Investigation',
    owner_display_name: 'Owner',
    messages: (ids.length ? ids : ['m1']).map(msg),
  };
}

// Drive the last opened socket's open handler (the hook wires it synchronously).
function openLast() {
  act(() => {
    sockets[sockets.length - 1].onopen?.();
  });
}

function closeLast(code: number) {
  act(() => {
    sockets[sockets.length - 1].onclose?.({ code });
  });
}

describe('useSharedSocket', () => {
  beforeEach(() => {
    mockGet.mockReset();
    sockets = [];
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('loads the transcript then opens the token-authed socket', async () => {
    mockGet.mockResolvedValue(snapshot());
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    await waitFor(() => expect(sockets).toHaveLength(1));
    expect(sockets[0].url).toBe('ws://test/ws/shared/TOK');
    expect(sockets[0].protocols).toBe('inkwest');
    await waitFor(() => expect(result.current.status).toBe('ready'));

    openLast();
    expect(result.current.connection).toBe('live');
    unmount();
  });

  // The regression: a transient close used to be swallowed, freezing the guest's
  // view. Reconnecting must also RE-READ the transcript — the server only replays
  // the turn in flight, so a turn that finished while we were away is REST-only.
  it('re-fetches the transcript and reopens the socket after a transient close', async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValue(snapshot());
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    expect(sockets).toHaveLength(1);
    openLast();

    closeLast(1006); // abnormal closure: proxy idle-timeout, sleep, network blip
    expect(result.current.connection).toBe('reconnecting');

    await act(async () => void (await vi.advanceTimersByTimeAsync(5000))); // ~3s backoff
    expect(sockets).toHaveLength(2);

    const readsBeforeOpen = mockGet.mock.calls.length;
    openLast();
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    // The reattached socket re-reads: whatever completed while it was down is
    // DB-only, since the server replays nothing older than the turn in flight.
    expect(mockGet.mock.calls.length).toBe(readsBeforeOpen + 1);
    expect(result.current.connection).toBe('live');
    expect(result.current.status).toBe('ready');
    unmount();
  });

  it('treats a revoke while viewing as terminal — no reconnect', async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValue(snapshot());
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast();
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    const reads = mockGet.mock.calls.length;
    closeLast(4012);

    await act(async () => void (await vi.advanceTimersByTimeAsync(60000)));
    expect(result.current.status).toBe('revoked');
    expect(sockets).toHaveLength(1);
    expect(mockGet.mock.calls.length).toBe(reads); // no reconnect, no further reads
    unmount();
  });

  it('stops retrying when the link is at its concurrent-guest cap', async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValue(snapshot());
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast();
    closeLast(4009);

    await act(async () => void (await vi.advanceTimersByTimeAsync(60000)));
    expect(result.current.connection).toBe('stale');
    expect(sockets).toHaveLength(1);
    unmount();
  });

  it('reports a 404 as revoked', async () => {
    mockGet.mockRejectedValue(new ApiError(404, 'This share link is invalid or has been revoked'));
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    await waitFor(() => expect(result.current.status).toBe('revoked'));
    expect(sockets).toHaveLength(0);
    unmount();
  });

  // A backend blip must not tell an external viewer their link is dead.
  it('keeps a non-404 load failure retryable and distinct from revoked', async () => {
    vi.useFakeTimers();
    mockGet.mockRejectedValue(new Error('gateway down'));
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    // act() so the state update from the rejected load is flushed by React.
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    expect(result.current.status).toBe('error');

    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    expect(mockGet.mock.calls.length).toBeGreaterThan(1); // still trying

    // …and it self-heals once the backend answers again.
    mockGet.mockResolvedValue(snapshot());
    await act(async () => void (await vi.advanceTimersByTimeAsync(10000)));
    expect(result.current.status).toBe('ready');
    unmount();
  });

  it('marks the view stale once the reconnect budget is spent', async () => {
    vi.useFakeTimers();
    mockGet.mockRejectedValue(new Error('gateway down'));
    const { result, unmount } = renderHook(() => useSharedSocket('TOK'));

    // Drive attempts across >10 min of virtual time (RECONNECT_BUDGET_MS).
    for (let i = 0; i < 40; i++) {
      await act(async () => void (await vi.advanceTimersByTimeAsync(30000)));
    }
    expect(result.current.connection).toBe('stale');
    unmount();
  });

  // The server replays the turn in flight and nothing older, so the transcript has
  // to be read AFTER the socket registers: a turn that finished in between is
  // DB-only, and a socket that stays healthy would never go back for it.
  it('re-reads the transcript once the socket is live', async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValueOnce(snapshot('m1')); // read at T0: m2 still running
    mockGet.mockResolvedValue(snapshot('m1', 'm2')); // by T1 it has finished

    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast();
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));

    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['m1', 'm2']);
    unmount();
  });

  function frame(sock: FakeWS, payload: Record<string, unknown>) {
    act(() => sock.onmessage?.({ data: JSON.stringify(payload) }));
  }

  // The server replays nothing for a turn that ended while the socket was down, so
  // the partial bubble + spinner from the previous socket would otherwise outlive it
  // under the completed answer the re-read brings in.
  it('drops the previous socket\'s partial turn on reopen', async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValueOnce(snapshot('u1')).mockResolvedValue(snapshot('u1', 'a1'));
    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast();
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    frame(sockets[0], { type: 'stream_resumed' });
    frame(sockets[0], { type: 'text_delta', content: 'partial' });
    expect(useChatStore.getState().isStreaming).toBe(true);

    closeLast(1006); // dropped mid-turn; the turn finishes while we are away
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    openLast(); // no active turn → the server sends nothing
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));

    const s = useChatStore.getState();
    expect(s.messages.map((m) => m.id)).toEqual(['u1', 'a1']);
    expect(s.isStreaming).toBe(false);
    expect(s.streamingBlocks).toHaveLength(0);
    unmount();
  });

  // The post-open read races the live stream: a `done` (and the user_message before
  // it) landing while that read is in flight would be overwritten by its older
  // transcript. The answer is persisted before `done`, so a fresh read has it.
  it('re-reads at done when a read was in flight, so the finished turn survives', async () => {
    vi.useFakeTimers();
    let resolveSlow: (v: unknown) => void = () => {};
    mockGet
      .mockResolvedValueOnce(snapshot('u1'))
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            resolveSlow = r;
          })
      )
      .mockResolvedValue(snapshot('u1', 'u2', 'a2'));
    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast(); // slow post-open read starts
    frame(sockets[0], { type: 'user_message', content: 'u2' });
    frame(sockets[0], { type: 'stream_resumed' });
    frame(sockets[0], { type: 'text_delta', content: 'a2' });
    frame(sockets[0], { type: 'done' });
    expect(useChatStore.getState().messages).toHaveLength(3); // u1 + peer u2 + local a2

    const reads = mockGet.mock.calls.length;
    await act(async () => {
      resolveSlow(snapshot('u1')); // superseded: must not win
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(mockGet.mock.calls.length).toBe(reads); // the re-read was issued at `done`
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['u1', 'u2', 'a2']);
    unmount();
  });

  it('re-reads at done when a user_message landed during an earlier read', async () => {
    vi.useFakeTimers();
    let resolveSlow: (v: unknown) => void = () => {};
    mockGet
      .mockResolvedValueOnce(snapshot('u1'))
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            resolveSlow = r;
          })
      )
      .mockResolvedValue(snapshot('u1', 'u2', 'a2'));
    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast();
    frame(sockets[0], { type: 'user_message', content: 'u2' });
    await act(async () => {
      resolveSlow(snapshot('u1')); // predates u2: clobbers the peer line
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['u1']);

    frame(sockets[0], { type: 'text_delta', content: 'a2' });
    frame(sockets[0], { type: 'done' }); // no read in flight now, but the flag is set
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['u1', 'u2', 'a2']);
    unmount();
  });

  it('aborts an in-flight read on unmount', async () => {
    let signal: AbortSignal | undefined;
    mockGet.mockImplementation((_token: string, s: AbortSignal) => {
      signal = s;
      return new Promise(() => {}); // never settles: the read is still in flight
    });

    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal!.aborted).toBe(false);

    unmount();
    expect(signal!.aborted).toBe(true);
  });

  it('a superseded slow read cannot overwrite a fresher transcript', async () => {
    vi.useFakeTimers();
    let resolveSlow: (v: unknown) => void = () => {};
    mockGet
      .mockResolvedValueOnce(snapshot('m1'))
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            resolveSlow = r;
          })
      )
      .mockResolvedValue(snapshot('m1', 'm2'));

    const { unmount } = renderHook(() => useSharedSocket('TOK'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    openLast(); // socket 1 opens -> slow read starts
    closeLast(1006);
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    openLast(); // socket 2 opens -> fresh read
    await act(async () => void (await vi.advanceTimersByTimeAsync(1)));
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['m1', 'm2']);

    // The stale read finally answers, long after it was superseded.
    await act(async () => {
      resolveSlow(snapshot('m1'));
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['m1', 'm2']);
    unmount();
  });
});
