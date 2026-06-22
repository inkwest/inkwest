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

  it('humanizes a valid ISO-8601 timestamp', () => {
    const out = formatBuildTime('2026-06-22T10:00:00Z');
    expect(out).not.toBeNull();
    expect(out).not.toContain('Invalid');
    // Locale/timezone vary in CI, but the year is always rendered.
    expect(out).toContain('2026');
  });
});
