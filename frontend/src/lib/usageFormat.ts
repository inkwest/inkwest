/** Shared formatting for the usage reports (admin + self-service). */

import { formatTokenCount } from '@/lib/utils';

// Compact token counts: 1.2M / 4.5k / 123. Delegates to the badge formatter
// (lib/utils.ts::formatTokenCount) — one implementation, so the same figure can never
// show two spellings between a table and a badge.
export const formatTokens = formatTokenCount;

// USD cost: 4 decimals for sub-cent non-zero amounts, 2 otherwise.
export function formatCost(cost: number | string): string {
  const n = typeof cost === 'string' ? parseFloat(cost) : cost;
  return n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

// Headline cost tiles (Today's/Total Cost): always exactly 2 decimals — the
// sub-cent precision above belongs in the per-row tables, not the summary figures.
export function formatCostTotal(cost: number | string): string {
  const n = typeof cost === 'string' ? parseFloat(cost) : cost;
  return `$${(Number.isFinite(n) ? n : 0).toFixed(2)}`;
}

// Chart series keys → display labels, shared by the self-service and admin usage charts.
export const TOKEN_SERIES_LABELS: Record<string, string> = {
  input_tokens: 'Input tokens',
  output_tokens: 'Output tokens',
  cache_read_tokens: 'Cache read',
  cache_write_tokens: 'Cache write',
  cost_usd: 'Cost',
};
