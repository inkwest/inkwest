import { describe, it, expect } from 'vitest';
import { sortSessions } from './sessionUsage';
import type { SessionUsage } from '@/types/admin';

const row = (title: string, cache_read_tokens?: number): SessionUsage => ({
  session_id: title,
  title,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens,
  total_tokens: 0,
  cost_usd: 0,
  request_count: 0,
  first_request: null,
  last_request: null,
});

describe('sortSessions', () => {
  const rows = [row('b', 5), row('a'), row('c', 1)];

  it('keeps server order when no key is set', () => {
    const out = sortSessions(rows, { key: null, dir: 'asc' });
    expect(out.map((r) => r.title)).toEqual(['b', 'a', 'c']);
  });

  it('sorts titles alphabetically in both directions', () => {
    expect(sortSessions(rows, { key: 'title', dir: 'asc' }).map((r) => r.title)).toEqual(['a', 'b', 'c']);
    expect(sortSessions(rows, { key: 'title', dir: 'desc' }).map((r) => r.title)).toEqual(['c', 'b', 'a']);
  });

  it('treats a missing number as 0 and does not mutate its input', () => {
    const out = sortSessions(rows, { key: 'cache_read_tokens', dir: 'asc' });
    expect(out.map((r) => r.title)).toEqual(['a', 'c', 'b']);
    expect(rows.map((r) => r.title)).toEqual(['b', 'a', 'c']);
  });
});
