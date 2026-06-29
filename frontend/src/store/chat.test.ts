import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore, groupServerMessages } from './chat';
import { ContentBlock, Message } from '@/types/api';

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

  it('folds persisted tool_result rows into the assistant turn on apply', () => {
    useChatStore.getState().applyServerMessages([
      msg('u', 'user'),
      assistantWithTools('a', [{ type: 'tool_use', id: 't1', name: 'kubectl', input: {} }]),
      toolResultRow('r', [{ type: 'tool_result', tool_use_id: 't1', content: 'ok', is_error: false }]),
    ]);
    const messages = useChatStore.getState().messages;
    expect(messages.map((m) => m.id)).toEqual(['u', 'a']);
    expect((messages[1].content as ContentBlock[]).map((b) => b.type)).toEqual([
      'tool_use',
      'tool_result',
    ]);
  });
});

describe('chatStore message queue', () => {
  beforeEach(() => {
    useChatStore.setState({ queuedMessages: [] });
  });

  it('enqueues messages in order, dropping empty attachment lists', () => {
    const { enqueueMessage } = useChatStore.getState();
    enqueueMessage('first', []);
    enqueueMessage('second', [{ filename: 'f', original_name: 'f', content_type: 'text/plain', size: 1 }]);
    const queued = useChatStore.getState().queuedMessages;
    expect(queued.map((m) => m.content)).toEqual(['first', 'second']);
    expect(queued[0].attachments).toBeUndefined();
    expect(queued[1].attachments).toHaveLength(1);
    expect(queued[0].id).not.toEqual(queued[1].id);
  });

  it('updates a queued message content by id, leaving others untouched', () => {
    const { enqueueMessage, updateQueuedMessage } = useChatStore.getState();
    enqueueMessage('a');
    enqueueMessage('b');
    const [first] = useChatStore.getState().queuedMessages;
    updateQueuedMessage(first.id, 'edited');
    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['edited', 'b']);
  });

  it('removes a queued message by id and clears the whole queue', () => {
    const { enqueueMessage, removeQueuedMessage, clearQueue } = useChatStore.getState();
    enqueueMessage('a');
    enqueueMessage('b');
    const [a] = useChatStore.getState().queuedMessages;
    removeQueuedMessage(a.id);
    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['b']);
    clearQueue();
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it('drops the queue on resetChat (session switch)', () => {
    useChatStore.getState().enqueueMessage('stale');
    useChatStore.getState().resetChat();
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });
});

function assistantWithTools(id: string, blocks: ContentBlock[]): Message {
  return { id, session_id: 's1', role: 'assistant', content: blocks, created_at: '2026-06-10T00:00:00Z' };
}

function toolResultRow(id: string, blocks: ContentBlock[]): Message {
  return { id, session_id: 's1', role: 'tool_result', content: blocks, created_at: '2026-06-10T00:00:00Z' };
}

describe('groupServerMessages', () => {
  it('folds a tool_result row into the preceding assistant message', () => {
    const out = groupServerMessages([
      assistantWithTools('a', [
        { type: 'text', text: 'hi' },
        { type: 'tool_use', id: 't1', name: 'kubectl', input: {} },
      ]),
      toolResultRow('r', [{ type: 'tool_result', tool_use_id: 't1', content: 'ok', is_error: false }]),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe('a');
    expect((out[0].content as ContentBlock[]).map((b) => b.type)).toEqual([
      'text',
      'tool_use',
      'tool_result',
    ]);
  });

  it('fuses a whole multi-round turn into one assistant bubble (CLI parity)', () => {
    const out = groupServerMessages([
      msg('u', 'user'),
      assistantWithTools('a1', [
        { type: 'text', text: 'first' },
        { type: 'tool_use', id: 't1', name: 'x', input: {} },
      ]),
      toolResultRow('r1', [{ type: 'tool_result', tool_use_id: 't1', content: 'A', is_error: false }]),
      assistantWithTools('a2', [{ type: 'tool_use', id: 't2', name: 'y', input: {} }]),
      toolResultRow('r2', [{ type: 'tool_result', tool_use_id: 't2', content: 'B', is_error: false }]),
    ]);
    // One user bubble + one fused assistant bubble; the fused bubble keeps the
    // first assistant row's id and carries every round's blocks in turn order.
    expect(out.map((m) => m.id)).toEqual(['u', 'a1']);
    expect((out[1].content as ContentBlock[]).map((b) => b.type)).toEqual([
      'text',
      'tool_use',
      'tool_result',
      'tool_use',
      'tool_result',
    ]);
    // Absorbed row ids are tracked so a search deep-link to any round resolves.
    expect(out[1].mergedIds).toEqual(['r1', 'a2', 'r2']);
  });

  it('fuses a trailing text-only assistant round into the same bubble', () => {
    const out = groupServerMessages([
      msg('u', 'user'),
      assistantWithTools('a1', [{ type: 'tool_use', id: 't1', name: 'x', input: {} }]),
      toolResultRow('r1', [{ type: 'tool_result', tool_use_id: 't1', content: 'A', is_error: false }]),
      // Final round is a plain text answer persisted as a string-content row.
      { id: 'a2', session_id: 's1', role: 'assistant', content: 'done', created_at: '2026-06-10T00:00:00Z' },
    ]);
    expect(out.map((m) => m.id)).toEqual(['u', 'a1']);
    expect((out[1].content as ContentBlock[]).map((b) => b.type)).toEqual([
      'tool_use',
      'tool_result',
      'text',
    ]);
  });

  it('leaves a tool_result row standalone when no assistant precedes it', () => {
    const out = groupServerMessages([
      toolResultRow('r', [{ type: 'tool_result', tool_use_id: 't1', content: 'ok', is_error: false }]),
    ]);
    expect(out.map((m) => m.id)).toEqual(['r']);
  });

  it('does not mutate the input messages', () => {
    const assistant = assistantWithTools('a', [{ type: 'tool_use', id: 't1', name: 'x', input: {} }]);
    groupServerMessages([
      assistant,
      toolResultRow('r', [{ type: 'tool_result', tool_use_id: 't1', content: 'ok', is_error: false }]),
    ]);
    expect((assistant.content as ContentBlock[])).toHaveLength(1);
  });
});
