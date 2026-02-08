import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWebSocket } from './useWebSocket';
import { useChatStore } from '@/store/chat';

vi.mock('@/services/websocket', () => {
  class MockChatWebSocket {
    private listeners: Set<(event: unknown) => void> = new Set();

    constructor(public sessionId: string) {}

    connect() {
      setTimeout(() => {
        for (const listener of this.listeners) {
          listener({ type: 'connected' });
        }
      }, 0);
    }

    disconnect() {
      for (const listener of this.listeners) {
        listener({ type: 'disconnected' });
      }
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
  }

  return { ChatWebSocket: MockChatWebSocket };
});

describe('useWebSocket', () => {
  beforeEach(() => {
    useChatStore.getState().clearStreamingMessage();
    useChatStore.setState({
      messages: [],
      streamingMessage: '',
      isConnected: false,
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
});
