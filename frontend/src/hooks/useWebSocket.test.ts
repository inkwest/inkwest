import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWebSocket } from './useWebSocket';
import { useChatStore } from '@/store/chat';
import { useSessionsStore } from '@/store/sessions';
import { ChatWebSocket } from '@/services/websocket';
import type { Message, Session } from '@/types/api';

const failedMessage: Message = {
  id: 'm1',
  session_id: 'session-1',
  role: 'user',
  content: 'analyze the cluster',
  created_at: '2026-01-01T00:00:00Z',
  status: 'error',
  is_retryable: true,
  error_message: 'boom',
};

vi.mock('@/services/websocket', () => {
  class MockChatWebSocket {
    // Track instances so tests can drive lifecycle frames into the live hook.
    static instances: MockChatWebSocket[] = [];
    private listeners: Set<(event: unknown) => void> = new Set();

    constructor(public sessionId: string) {
      MockChatWebSocket.instances.push(this);
    }

    emit(event: unknown) {
      for (const listener of this.listeners) listener(event);
    }

    connect() {
      setTimeout(() => this.emit({ type: 'connected' }), 0);
    }

    disconnect() {
      this.emit({ type: 'disconnected' });
    }

    subscribe(fn: (event: unknown) => void) {
      this.listeners.add(fn);
      return () => {
        this.listeners.delete(fn);
      };
    }

    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    sendMessage(_content: string) {
      // no-op in mock
    }

    // Mirrors the real service: returns whether the frame actually went out.
    // Tests flip this to simulate a closed/reconnecting socket.
    retryShouldSend = true;
    sendRetry() {
      return this.retryShouldSend;
    }
  }

  return { ChatWebSocket: MockChatWebSocket };
});

// The mock above replaces the real ChatWebSocket; cast to reach its test hooks.
const MockWS = ChatWebSocket as unknown as {
  instances: Array<{ emit: (event: unknown) => void }>;
};

describe('useWebSocket', () => {
  beforeEach(() => {
    MockWS.instances.length = 0;
    useChatStore.getState().clearStreamingMessage();
    useChatStore.setState({
      messages: [],
      streamingMessage: '',
      streamingBlocks: [],
      queuedMessages: [],
      isConnected: false,
      isReconnecting: false,
      isStreaming: false,
      resumingTurn: false,
      error: null,
    });
  });

  it('returns sendMessage function', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));
    expect(typeof result.current.sendMessage).toBe('function');
  });

  it('exposes isConnected state', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));
    expect(typeof result.current.isConnected).toBe('boolean');
  });

  it('exposes isStreaming state', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));
    expect(typeof result.current.isStreaming).toBe('boolean');
  });

  it('exposes error state', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));
    expect(result.current.error).toBeNull();
  });

  it('sendMessage adds user message to store', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));

    act(() => {
      result.current.sendMessage('hello');
    });

    const messages = useChatStore.getState().messages;
    expect(messages.length).toBe(1);
    expect(messages[0].role).toBe('user');
    expect(messages[0].content).toBe('hello');
  });

  it('sendMessage sets isStreaming to true', () => {
    const { result } = renderHook(() => useWebSocket('session-1'));

    act(() => {
      result.current.sendMessage('test');
    });

    expect(useChatStore.getState().isStreaming).toBe(true);
  });

  it('sendMessage optimistically titles an untitled session (sidebar surfacing)', () => {
    useSessionsStore.setState({
      sessions: [{ id: 'session-1', title: undefined } as Session],
      currentSession: null,
    });
    const { result } = renderHook(() => useWebSocket('session-1'));

    act(() => {
      result.current.sendMessage('Investigate the GitLab 500s');
    });

    expect(useSessionsStore.getState().sessions[0].title).toBe('Investigate the GitLab 500s');
  });

  it('retryMessage clears the error and starts streaming when the frame is sent', () => {
    useChatStore.setState({ messages: [{ ...failedMessage }] });
    const { result } = renderHook(() => useWebSocket('session-1'));

    act(() => {
      result.current.retryMessage('m1');
    });

    const msg = useChatStore.getState().messages[0];
    expect(msg.status).toBe('success');
    expect(msg.is_retryable).toBe(false);
    expect(useChatStore.getState().isStreaming).toBe(true);
  });

  it("'reconnecting' toggles isReconnecting; 'connected' clears it", () => {
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[MockWS.instances.length - 1];

    act(() => ws.emit({ type: 'reconnecting', state: true }));
    expect(useChatStore.getState().isReconnecting).toBe(true);

    // Retries exhausted / terminal close — the banner clears, leaving "Disconnected".
    act(() => ws.emit({ type: 'reconnecting', state: false }));
    expect(useChatStore.getState().isReconnecting).toBe(false);

    // A successful reconnect clears the flag even mid-retry.
    act(() => ws.emit({ type: 'reconnecting', state: true }));
    act(() => ws.emit({ type: 'connected' }));
    expect(useChatStore.getState().isConnected).toBe(true);
    expect(useChatStore.getState().isReconnecting).toBe(false);
  });

  it('flushes the queue as one combined message when the turn ends (done)', () => {
    const sendSpy = vi.spyOn(ChatWebSocket.prototype, 'sendMessage');
    useChatStore.setState({
      messages: [],
      streamingBlocks: [],
      isStreaming: true,
      queuedMessages: [
        { id: 'q1', content: 'check the logs' },
        { id: 'q2', content: 'and the deploys' },
      ],
    });
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[MockWS.instances.length - 1];

    act(() => ws.emit({ type: 'done', metadata: {} }));

    // Combined into one turn (joined by blank line), queue drained, new turn started.
    expect(sendSpy).toHaveBeenCalledWith('check the logs\n\nand the deploys', undefined);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
    expect(useChatStore.getState().isStreaming).toBe(true);
    const messages = useChatStore.getState().messages;
    expect(messages[messages.length - 1].content).toBe('check the logs\n\nand the deploys');

    sendSpy.mockRestore();
  });

  it('flushes the queue and clears the banner even when the turn errors', () => {
    const sendSpy = vi.spyOn(ChatWebSocket.prototype, 'sendMessage');
    useChatStore.setState({
      messages: [],
      streamingBlocks: [],
      isStreaming: true,
      queuedMessages: [{ id: 'q1', content: 'retry the check' }],
    });
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[MockWS.instances.length - 1];

    act(() => ws.emit({ type: 'error', content: 'boom' }));

    expect(sendSpy).toHaveBeenCalledWith('retry the check', undefined);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
    expect(useChatStore.getState().error).toBeNull();
    expect(useChatStore.getState().isStreaming).toBe(true);

    sendSpy.mockRestore();
  });

  it('retryMessage is a no-op when the socket cannot send (dropped frame)', () => {
    // A closed/reconnecting socket drops retry (it is not queued). Faking success
    // would strand the UI in a perpetual streaming state — guard against that.
    const spy = vi.spyOn(ChatWebSocket.prototype, 'sendRetry').mockReturnValue(false);
    useChatStore.setState({ messages: [{ ...failedMessage }], isStreaming: false });
    const { result } = renderHook(() => useWebSocket('session-1'));

    act(() => {
      result.current.retryMessage('m1');
    });

    const msg = useChatStore.getState().messages[0];
    expect(msg.status).toBe('error');
    expect(msg.is_retryable).toBe(true);
    expect(useChatStore.getState().isStreaming).toBe(false);
    spy.mockRestore();
  });

  it('mcp_tool_ask stacks a pending ask; mcp_tool_ask_resolved clears it', async () => {
    const { useMcpStore } = await import('@/store/mcp');
    useMcpStore.setState({ pendingAsks: [] });
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[0];

    act(() =>
      ws.emit({
        type: 'mcp_tool_ask',
        ask_id: 'a1',
        mcp_key: 'notion',
        tool: 'search',
        session_id: 'session-1',
      })
    );
    expect(useMcpStore.getState().pendingAsks).toEqual([
      { askId: 'a1', mcpKey: 'notion', tool: 'search', sessionId: 'session-1' },
    ]);

    // Duplicate frame (reconnect replay) must not double the banner.
    act(() =>
      ws.emit({
        type: 'mcp_tool_ask',
        ask_id: 'a1',
        mcp_key: 'notion',
        tool: 'search',
        session_id: 'session-1',
      })
    );
    expect(useMcpStore.getState().pendingAsks).toHaveLength(1);

    act(() => ws.emit({ type: 'mcp_tool_ask_resolved', ask_id: 'a1', verdict: 'deny' }));
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
  });

  it('mcp_tool_ask from a pre-session_id backend downgrades to an empty sessionId', async () => {
    const { useMcpStore } = await import('@/store/mcp');
    useMcpStore.setState({ pendingAsks: [] });
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[0];
    act(() => ws.emit({ type: 'mcp_tool_ask', ask_id: 'a1', mcp_key: 'notion', tool: 'search' }));
    // '' hides the conversation scope in the decision bar instead of misrouting the write.
    expect(useMcpStore.getState().pendingAsks).toEqual([
      { askId: 'a1', mcpKey: 'notion', tool: 'search', sessionId: '' },
    ]);
  });

  it('mcp_tool_ask without required fields is ignored', async () => {
    const { useMcpStore } = await import('@/store/mcp');
    useMcpStore.setState({ pendingAsks: [] });
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[0];
    act(() => ws.emit({ type: 'mcp_tool_ask', ask_id: 'a1' })); // no key/tool
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
  });

  it('stream_resumed marks the turn resuming; done drops the persisted in-progress copy', () => {
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[0];
    // Reload state: the user turn + its persisted in-progress assistant already in the transcript.
    useChatStore.setState({
      messages: [
        { id: 'u1', session_id: 'session-1', role: 'user', content: 'go', created_at: '2026-01-01T00:00:00Z' },
        {
          id: 'a-partial',
          session_id: 'session-1',
          role: 'assistant',
          content: [{ type: 'text', text: 'partial' }],
          created_at: '2026-01-01T00:00:01Z',
        },
      ],
    });
    act(() => ws.emit({ type: 'stream_resumed' }));
    expect(useChatStore.getState().resumingTurn).toBe(true);
    // Replay fills the buffer, then the turn completes.
    act(() => ws.emit({ type: 'text_delta', content: 'partial and more' }));
    act(() => ws.emit({ type: 'done', metadata: {} }));

    const assistants = useChatStore.getState().messages.filter((m) => m.role === 'assistant');
    expect(assistants).toHaveLength(1); // stale partial dropped, completed turn appended once
    expect(useChatStore.getState().resumingTurn).toBe(false);
  });

  it('clears resumingTurn on disconnect (re-set on reconnect if the turn is still running)', () => {
    renderHook(() => useWebSocket('session-1'));
    const ws = MockWS.instances[0];
    act(() => ws.emit({ type: 'stream_resumed' }));
    expect(useChatStore.getState().resumingTurn).toBe(true);
    act(() => ws.emit({ type: 'disconnected' }));
    expect(useChatStore.getState().resumingTurn).toBe(false);
  });
});
