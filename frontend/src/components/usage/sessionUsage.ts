import type { SortState } from '@/lib/tableSort';
import type { SessionUsage } from '@/types/admin';

// Kept out of SessionUsageTable.tsx: react-refresh needs a component file to export only components.
export type SessionSortKey =
  | 'title'
  | 'request_count'
  | 'input_tokens'
  | 'total_tokens'
  | 'cache_read_tokens'
  | 'cost_usd';

// Client-side tristate sort; null key ⇒ preserve server order (costliest-first).
export function sortSessions(
  sessions: SessionUsage[],
  sort: SortState<SessionSortKey>,
): SessionUsage[] {
  const key = sort.key;
  if (key === null) return sessions;
  const dir = sort.dir === 'asc' ? 1 : -1;
  return [...sessions].sort((a, b) => {
    if (key === 'title') return dir * (a.title || '').localeCompare(b.title || '');
    return dir * (((a[key] as number) || 0) - ((b[key] as number) || 0));
  });
}
