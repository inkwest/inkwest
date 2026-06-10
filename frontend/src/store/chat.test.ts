import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from './chat';
import { Message } from '@/types/api';

function msg(id: string, role: 'user' | 'assistant' = 'user'): Message {
  return {
    id,
    session_id: 's1',
    role,
    content: `content ${id}`,
    created_at: '2026-06-10T00:00:00Z',
  };
}

describe('chatStore.applyServerMessages', () => {
  beforeEach(() => {
    useChatStore.setState({ messages: [], isStreaming: false });
  });

  it('applies a server snapshot when idle', () => {
    useChatStore.getState().applyServerMessages([msg('a'), msg('b', 'assistant')]);
    expect(useChatStore.getState().messages).toHaveLength(2);
  });

  it('keeps the optimistic message when a stale snapshot arrives mid-turn', () => {
    const { addMessage, setIsStreaming, applyServerMessages } = useChatStore.getState();
    addMessage(msg('optimistic'));
    setIsStreaming(true);
    // Refetch resolved with a snapshot taken before the message was persisted.
    applyServerMessages([]);
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['optimistic']);
  });

  it('replaces the optimistic message with the persisted one mid-turn', () => {
    const { addMessage, setIsStreaming, applyServerMessages } = useChatStore.getState();
    addMessage(msg('optimistic'));
    setIsStreaming(true);
    applyServerMessages([msg('persisted')]);
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['persisted']);
  });

  it('applies a shorter snapshot when not streaming (server is the truth)', () => {
    const { addMessage, applyServerMessages } = useChatStore.getState();
    addMessage(msg('a'));
    addMessage(msg('b', 'assistant'));
    applyServerMessages([msg('a')]);
    expect(useChatStore.getState().messages.map((m) => m.id)).toEqual(['a']);
  });
});
