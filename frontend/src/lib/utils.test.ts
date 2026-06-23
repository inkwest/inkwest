import { describe, it, expect } from 'vitest';
import { formatBuildTime } from './utils';

describe('formatBuildTime', () => {
  it('returns null when there is nothing real to show', () => {
    expect(formatBuildTime(null)).toBeNull();
    expect(formatBuildTime('')).toBeNull();
    expect(formatBuildTime('unknown')).toBeNull();
  });

  it('falls back to the raw string for an unparseable value', () => {
    expect(formatBuildTime('not-a-date')).toBe('not-a-date');
  });

  it('renders local-time YYYY-MM-DD HH:MM:SS (24h)', () => {
    const out = formatBuildTime('2026-06-22T10:00:00Z');
    expect(out).not.toBeNull();
    expect(out).not.toContain('Invalid');
    // Local TZ varies in CI, so assert the shape rather than exact value.
    expect(out).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});
