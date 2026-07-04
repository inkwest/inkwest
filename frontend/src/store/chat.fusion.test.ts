// Bubble-fusion token-field retention (separate file: store/chat.test.ts is owned by an
// in-flight fix). The backend stamps a turn's token totals onto the turn's FIRST assistant
// row precisely because groupServerMessages keeps the first row's scalar fields when fusing
// consecutive assistant/tool_result rows — this pins that invariant so a merge refactor
// (e.g. spreading the newer row) can't silently blank the per-bubble token line.
import { describe, it, expect } from 'vitest';
import { groupServerMessages } from './chat';
import type { Message } from '@/types/api';

const first: Message = {
  id: 'm1',
  session_id: 's1',
  role: 'assistant',
  content: [{ type: 'text', text: 'looking...' }],
  created_at: '2026-07-04T10:00:00Z',
  input_tokens: 7,
  output_tokens: 16,
  cache_read_tokens: 12608,
  cache_write_tokens: 0,
};

const toolResult: Message = {
  id: 'm2',
  session_id: 's1',
  role: 'tool_result',
  content: [{ type: 'tool_result', tool_use_id: 't1', content: 'pod/x' }],
  created_at: '2026-07-04T10:00:05Z',
};

// A later flush of the same turn — carries NO token fields (only the first row is stamped).
const trailing: Message = {
  id: 'm3',
  session_id: 's1',
  role: 'assistant',
  content: [{ type: 'text', text: 'done.' }],
  created_at: '2026-07-04T10:00:10Z',
};

describe('groupServerMessages token-field retention', () => {
  it('a fused multi-flush turn keeps the FIRST assistant row token fields', () => {
    const out = groupServerMessages([first, toolResult, trailing]);
    expect(out).toHaveLength(1);
    const bubble = out[0];
    expect(bubble.input_tokens).toBe(7);
    expect(bubble.output_tokens).toBe(16);
    expect(bubble.cache_read_tokens).toBe(12608);
    expect(bubble.cache_write_tokens).toBe(0);
    // absorbed rows remain addressable for deep links
    expect(bubble.mergedIds).toEqual(['m2', 'm3']);
  });

  it('unstamped later rows never override the first row values with undefined', () => {
    const out = groupServerMessages([first, trailing]);
    expect(out[0].input_tokens).toBe(7);
    expect(out[0].cache_read_tokens).toBe(12608);
  });
});
