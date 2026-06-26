import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWebSocket } from './useWebSocket';
import { useChatStore } from '@/store/chat';
import { ChatWebSocket } from '@/services/websocket';
import type { Message } from '@/types/api';

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
      isConnected: false,
      isReconnecting: false,
      isStreaming: false,
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
});
