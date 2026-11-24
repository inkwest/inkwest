/** Shared formatting for the usage reports (admin + self-service). */

import { formatTokenCount } from '@/lib/utils';

// Compact token counts: 1.2M / 4.5k / 123. Delegates to the badge formatter
// (lib/utils.ts::formatTokenCount) — one implementation, so the same figure can never
// show two spellings between a table and a badge.
export const formatTokens = formatTokenCount;

// Combined "a / b" columns show both figures and sort by the first.
export const IN_OUT_TOOLTIP = 'Fresh input / output tokens. Sorts by input.';
export const CACHE_LABEL = 'Cache R / W';
export const CACHE_TOOLTIP =
  'Prompt-cache read / write tokens (reads billed ~0.1x input; writes ~1.25x, 2x for 1h TTL). Sorts by reads.';

// Missing figures (rows recorded before a column existed) render as 0.
export function formatTokenPair(a: number | undefined, b: number | undefined): string {
  return `${formatTokens(a || 0)} / ${formatTokens(b || 0)}`;
}

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

// Time-axis labels for the usage chart: time-of-day for short windows (hourly/daily
// buckets), ISO date otherwise.
export function formatTimeseriesLabel(iso: string, period: string): string {
  const d = new Date(iso);
  if (period === 'hourly' || period === 'daily')
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toISOString().slice(0, 10);
}

// Chart series keys → display labels, shared by the self-service and admin usage charts.
export const TOKEN_SERIES_LABELS: Record<string, string> = {
  input_tokens: 'Input tokens',
  output_tokens: 'Output tokens',
  cache_read_tokens: 'Cache read',
  cache_write_tokens: 'Cache write',
  cost_usd: 'Cost',
};
