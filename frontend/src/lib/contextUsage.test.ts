import { describe, it, expect } from 'vitest';
import { contextUsage, lastPromptTokens, shouldOfferCompaction } from './contextUsage';
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

describe('shouldOfferCompaction', () => {
  const user = msg({ id: 'u', role: 'user', content: 'q' });
  const boundary = msg({
    id: 'b',
    role: 'system',
    content: { type: 'session_event', subject: 'context', action: 'compacted' },
  });

  it('offers on a known window past the threshold, not before', () => {
    expect(shouldOfferCompaction([user, msg({ input_tokens: 150_000 })], 200_000)).toBe(true);
    expect(shouldOfferCompaction([user, msg({ input_tokens: 100_000 })], 200_000)).toBe(false);
  });

  it('falls back to an absolute prompt size when the window is unknown', () => {
    expect(shouldOfferCompaction([user, msg({ input_tokens: 150_000 })], null)).toBe(true);
    expect(shouldOfferCompaction([user, msg({ input_tokens: 10_000 })], null)).toBe(false);
  });

  it('goes quiet when the last boundary is the newest row worth summarizing', () => {
    expect(shouldOfferCompaction([user, msg({ input_tokens: 190_000 }), boundary], 200_000)).toBe(
      false
    );
  });

  it('offers again once a new turn lands after the boundary', () => {
    const after = [user, msg({ input_tokens: 190_000 }), boundary, user, msg({ id: 'a2', input_tokens: 190_000 })];
    expect(shouldOfferCompaction(after, 200_000)).toBe(true);
  });

  it('ignores session events when deciding there is something to compact', () => {
    const events = [
      boundary,
      msg({
        id: 'e',
        role: 'system',
        content: { type: 'session_event', subject: 'aws', action: 'granted' },
      }),
    ];
    expect(shouldOfferCompaction(events, 200_000)).toBe(false);
  });

  it('says no before any turn has reported usage', () => {
    expect(shouldOfferCompaction([user], 200_000)).toBe(false);
  });
});

describe('post-compaction readings', () => {
  const boundary = msg({
    id: 'b',
    role: 'system',
    content: { type: 'session_event', subject: 'context', action: 'compacted' },
  });

  it('reports no usage until a turn runs after the boundary', () => {
    // The summary turn's own prompt WAS the full pre-compaction context; reporting it
    // would leave the gauge red right after the user paid to reclaim that context.
    const rows = [msg({ id: 'u', role: 'user' }), msg({ input_tokens: 192_000 }), boundary];
    expect(contextUsage(rows, 200_000)).toBeNull();
    expect(lastPromptTokens(rows)).toBeNull();
  });

  it('does not re-offer compaction at a stale percentage', () => {
    // A row after the boundary with no fresh usage reading (stopped/errored turn) must not
    // resurrect the pre-compaction 96% and spend a turn to summarize one message.
    const rows = [
      msg({ id: 'u', role: 'user' }),
      msg({ input_tokens: 192_000 }),
      boundary,
      msg({ id: 'u2', role: 'user', content: 'next' }),
    ];
    expect(shouldOfferCompaction(rows, 200_000)).toBe(false);
  });

  it('reads again once a real turn lands after the boundary', () => {
    const rows = [
      msg({ id: 'u', role: 'user' }),
      msg({ input_tokens: 192_000 }),
      boundary,
      msg({ id: 'u2', role: 'user' }),
      msg({ id: 'a2', input_tokens: 20_000 }),
    ];
    expect(contextUsage(rows, 200_000)?.used).toBe(20_000);
    expect(shouldOfferCompaction(rows, 200_000)).toBe(false);
  });
});
