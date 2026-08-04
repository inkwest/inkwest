import { describe, it, expect } from 'vitest';
import { contextUsage } from './contextUsage';
import type { Message } from '@/types/api';

function msg(over: Partial<Message>): Message {
  return {
    id: over.id ?? 'm1',
    session_id: 's1',
    role: 'assistant',
    content: 'hi',
    created_at: '2026-08-03T10:00:00Z',
    ...over,
  } as Message;
}

describe('contextUsage', () => {
  it('sums fresh input + cache read + cache write against the window', () => {
    const usage = contextUsage(
      [msg({ input_tokens: 5000, cache_read_tokens: 120000, cache_write_tokens: 3000 })],
      200_000
    );
    expect(usage).toEqual({ used: 128_000, window: 200_000, percent: 64 });
  });

  it('returns null when the context window is unknown', () => {
    const m = [msg({ input_tokens: 5000, cache_read_tokens: 120000 })];
    expect(contextUsage(m, null)).toBeNull();
    expect(contextUsage(m, undefined)).toBeNull();
    expect(contextUsage(m, 0)).toBeNull();
  });

  it('returns null before any assistant turn has reported usage', () => {
    expect(contextUsage([], 200_000)).toBeNull();
    expect(contextUsage([msg({ role: 'user', input_tokens: null })], 200_000)).toBeNull();
  });

  it('uses the latest assistant turn, not the largest', () => {
    const usage = contextUsage(
      [
        msg({ id: 'a', input_tokens: 180_000 }),
        msg({ id: 'b', role: 'user' }),
        msg({ id: 'c', input_tokens: 20_000 }),
      ],
      200_000
    );
    expect(usage?.used).toBe(20_000);
  });

  it('skips assistant rows with no reported usage', () => {
    // A retry that errored before its first API reply must not blank a live gauge.
    const usage = contextUsage(
      [msg({ id: 'a', input_tokens: 100_000 }), msg({ id: 'b', status: 'error' })],
      200_000
    );
    expect(usage?.used).toBe(100_000);
  });

  it('caps the percentage at 100 when the count exceeds a stale window', () => {
    const usage = contextUsage([msg({ input_tokens: 300_000 })], 200_000);
    expect(usage?.percent).toBe(100);
    expect(usage?.used).toBe(300_000);
  });
});
