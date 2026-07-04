import { describe, it, expect } from 'vitest';
import { formatTokens, formatCost, formatCostTotal } from './usageFormat';
import { formatTokenCount } from './utils';

describe('formatTokens', () => {
  it('is the badge formatter (single spelling everywhere)', () => {
    expect(formatTokens).toBe(formatTokenCount);
  });

  it('pins the abbreviation boundaries', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1_000)).toBe('1.0k');
    expect(formatTokens(4_500)).toBe('4.5k');
    expect(formatTokens(999_949)).toBe('999.9k');
    expect(formatTokens(1_000_000)).toBe('1.0M');
    expect(formatTokens(1_234_567)).toBe('1.2M');
  });
});

describe('formatCost', () => {
  it('uses 4 decimals for sub-cent non-zero amounts, 2 otherwise', () => {
    expect(formatCost(0)).toBe('$0.00');
    expect(formatCost(0.0042)).toBe('$0.0042');
    expect(formatCost(0.01)).toBe('$0.01');
    expect(formatCost(1.239)).toBe('$1.24');
    expect(formatCost('0.005')).toBe('$0.0050');
  });
});

describe('formatCostTotal', () => {
  it('always exactly 2 decimals, non-finite input degrades to $0.00', () => {
    expect(formatCostTotal(5.291234)).toBe('$5.29');
    expect(formatCostTotal('3.1')).toBe('$3.10');
    expect(formatCostTotal(NaN)).toBe('$0.00');
    expect(formatCostTotal('not-a-number')).toBe('$0.00');
  });
});
