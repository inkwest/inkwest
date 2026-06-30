import { describe, it, expect } from 'vitest';
import { cycleSort, sortArrow, ariaSort, pageWindow, type SortState } from './tableSort';

describe('cycleSort', () => {
  it('starts a new column at ascending', () => {
    expect(cycleSort({ key: 'cost', dir: 'desc' }, 'name')).toEqual({ key: 'name', dir: 'asc' });
  });

  it('cycles the active column asc → desc → none', () => {
    const asc: SortState<string> = { key: 'name', dir: 'asc' };
    const desc = cycleSort(asc, 'name');
    expect(desc).toEqual({ key: 'name', dir: 'desc' });
    const none = cycleSort(desc, 'name');
    expect(none).toEqual({ key: null, dir: 'asc' });
  });
});

describe('sortArrow', () => {
  it('returns a two-char NBSP placeholder for an inactive column (no layout shift)', () => {
    expect(sortArrow({ key: 'a', dir: 'asc' }, 'b')).toBe('  ');
  });

  it('returns the directional arrow for the active column', () => {
    expect(sortArrow({ key: 'a', dir: 'asc' }, 'a')).toBe(' ↑');
    expect(sortArrow({ key: 'a', dir: 'desc' }, 'a')).toBe(' ↓');
  });
});

describe('ariaSort', () => {
  it('maps the active column to ARIA sort terms and inactive to none', () => {
    expect(ariaSort({ key: 'a', dir: 'asc' }, 'a')).toBe('ascending');
    expect(ariaSort({ key: 'a', dir: 'desc' }, 'a')).toBe('descending');
    expect(ariaSort({ key: 'a', dir: 'asc' }, 'b')).toBe('none');
  });
});

describe('pageWindow', () => {
  it('computes page count and slice bounds', () => {
    expect(pageWindow(25, 10, 0)).toEqual({ pageCount: 3, clamped: 0, start: 0, end: 10 });
    expect(pageWindow(25, 10, 2)).toEqual({ pageCount: 3, clamped: 2, start: 20, end: 30 });
  });

  it('clamps an out-of-range page into the last page', () => {
    expect(pageWindow(25, 10, 99)).toEqual({ pageCount: 3, clamped: 2, start: 20, end: 30 });
  });

  it('clamps a negative page to zero', () => {
    expect(pageWindow(25, 10, -5)).toEqual({ pageCount: 3, clamped: 0, start: 0, end: 10 });
  });

  it('always reports at least one page when empty', () => {
    expect(pageWindow(0, 10, 0)).toEqual({ pageCount: 1, clamped: 0, start: 0, end: 10 });
  });
});
