import { describe, it, expect, vi, afterEach } from 'vitest';
import { jitteredBackoff } from './backoff';

describe('jitteredBackoff', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('grows exponentially with the attempt (0-based)', () => {
    // Pin jitter to its midpoint (random=0.5 → factor 1.0) to read the raw curve.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    expect(jitteredBackoff(0, { baseMs: 500, maxMs: 5000 })).toBe(500);
    expect(jitteredBackoff(1, { baseMs: 500, maxMs: 5000 })).toBe(1000);
    expect(jitteredBackoff(2, { baseMs: 500, maxMs: 5000 })).toBe(2000);
  });

  it('caps at maxMs', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    expect(jitteredBackoff(20, { baseMs: 500, maxMs: 5000 })).toBe(5000);
  });

  it('applies ±25% jitter at the extremes', () => {
    const rnd = vi.spyOn(Math, 'random');
    rnd.mockReturnValue(0); // factor 0.75
    expect(jitteredBackoff(0, { baseMs: 1000, maxMs: 30000 })).toBe(750);
    rnd.mockReturnValue(0.999999); // factor ~1.25
    expect(jitteredBackoff(0, { baseMs: 1000, maxMs: 30000 })).toBe(1250);
  });
});
