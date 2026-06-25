import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from './chat';

describe('chat store — presence / typing / peer messages', () => {
  beforeEach(() => useChatStore.getState().resetChat());

  it('setPresence replaces the roster', () => {
    useChatStore.getState().setPresence([{ user_id: 'u1', display_name: 'Alice' }]);
    expect(useChatStore.getState().presence).toEqual([{ user_id: 'u1', display_name: 'Alice' }]);
  });

  it('setTyping adds then removes a user by id', () => {
    useChatStore.getState().setTyping('u1', 'Alice', true);
    expect(useChatStore.getState().typingUsers).toHaveLength(1);
    // Re-asserting true must not duplicate.
    useChatStore.getState().setTyping('u1', 'Alice', true);
    expect(useChatStore.getState().typingUsers).toHaveLength(1);
    useChatStore.getState().setTyping('u1', 'Alice', false);
    expect(useChatStore.getState().typingUsers).toHaveLength(0);
  });

  it('addPeerMessage ignores a duplicate id', () => {
    const msg = {
      id: 'm1',
      session_id: 's',
      role: 'user' as const,
      content: 'why is it down?',
      created_at: '',
    };
    useChatStore.getState().addPeerMessage(msg);
    useChatStore.getState().addPeerMessage(msg);
    expect(useChatStore.getState().messages.filter((m) => m.id === 'm1')).toHaveLength(1);
  });

  it('resetChat clears presence and typing', () => {
    useChatStore.getState().setPresence([{ user_id: 'u1', display_name: 'Alice' }]);
    useChatStore.getState().setTyping('u1', 'Alice', true);
    useChatStore.getState().resetChat();
    expect(useChatStore.getState().presence).toEqual([]);
    expect(useChatStore.getState().typingUsers).toEqual([]);
  });
});
