/**
 * Shared client-side table helpers: tristate column sort + front-only pagination math.
 * Used by the usage reports (admin UsagePage + self-service MyUsagePage) so both behave
 * identically and the logic is unit-testable in isolation.
 */

export type SortDir = 'asc' | 'desc';

// Tristate column sort: clicking a column cycles asc → desc → none (none ⇒ key null,
// table falls back to its default/server order). Clicking a different column starts at asc.
export type SortState<K extends string> = { key: K | null; dir: SortDir };

export function cycleSort<K extends string>(prev: SortState<K>, key: K): SortState<K> {
  if (prev.key !== key) return { key, dir: 'asc' };
  if (prev.dir === 'asc') return { key, dir: 'desc' };
  return { key: null, dir: 'asc' };
}

// Sort-direction arrow for a header. The separator is a non-breaking space so the arrow never
// wraps off the label; inactive columns reserve two NBSP of width so the row doesn't shift.
export function sortArrow<K extends string>(state: SortState<K>, key: K): string {
  if (state.key !== key) return '  ';
  return state.dir === 'asc' ? ' ↑' : ' ↓';
}

// ARIA sort state for a sortable column header (screen-reader semantics).
export function ariaSort<K extends string>(
  state: SortState<K>,
  key: K,
): 'ascending' | 'descending' | 'none' {
  if (state.key !== key) return 'none';
  return state.dir === 'asc' ? 'ascending' : 'descending';
}

// Front-only pagination window over an already-loaded array. `page` is clamped into range so a
// shrinking dataset (e.g. after a filter change) never leaves the view stranded past the last page.
export function pageWindow(
  total: number,
  pageSize: number,
  page: number,
): { pageCount: number; clamped: number; start: number; end: number } {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const clamped = Math.min(Math.max(0, page), pageCount - 1);
  const start = clamped * pageSize;
  return { pageCount, clamped, start, end: start + pageSize };
}
