import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui';
import { formatTokens } from '@/lib/usageFormat';
import type { UsageSummary } from '@/types/admin';

// The canonical UsageSummary minus the report-window fields the card doesn't render —
// Pick, not a parallel interface, so a UsageSummary rename can't silently drift this.
export type TokenBreakdownSummary = Pick<
  UsageSummary,
  | 'total_input_tokens'
  | 'total_output_tokens'
  | 'total_cache_read_tokens'
  | 'total_cache_write_tokens'
  | 'total_tokens'
  | 'request_count'
>;

/**
 * The 5-tile "Token Breakdown" card (fresh input/output with share bars, cache read/write,
 * requests). One implementation for MyUsagePage and the admin UsagePage — the tiles and
 * cache-rate tooltips must evolve in lockstep on both.
 */
export function TokenBreakdownCard({ summary }: { summary: TokenBreakdownSummary }) {
  return (
    <Card variant="bordered">
      <CardHeader>
        <CardTitle>Token Breakdown</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-6">
          <div>
            <p className="text-sm text-gray-400">Input Tokens</p>
            <p className="text-xl font-bold">{formatTokens(summary.total_input_tokens)}</p>
            <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500"
                style={{
                  width: `${summary.total_tokens > 0 ? (summary.total_input_tokens / summary.total_tokens) * 100 : 0}%`,
                }}
              />
            </div>
          </div>
          <div>
            <p className="text-sm text-gray-400">Output Tokens</p>
            <p className="text-xl font-bold">{formatTokens(summary.total_output_tokens)}</p>
            <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-500"
                style={{
                  width: `${summary.total_tokens > 0 ? (summary.total_output_tokens / summary.total_tokens) * 100 : 0}%`,
                }}
              />
            </div>
          </div>
          <div>
            <p className="text-sm text-gray-400" title="Served from the prompt cache (billed ~0.1x input rate)">Cache Read</p>
            <p className="text-xl font-bold">{formatTokens(summary.total_cache_read_tokens || 0)}</p>
          </div>
          <div>
            <p className="text-sm text-gray-400" title="Written to the prompt cache (billed ~1.25x input rate; 2x for 1h TTL)">Cache Write</p>
            <p className="text-xl font-bold">{formatTokens(summary.total_cache_write_tokens || 0)}</p>
          </div>
          <div>
            <p className="text-sm text-gray-400">Requests</p>
            <p className="text-xl font-bold">{summary.request_count}</p>
            <p
              className="text-sm text-gray-500 mt-2"
              title="Fresh input + output per request — cache tokens excluded, matching the Total Tokens figure"
            >
              ~{summary.request_count > 0 ? Math.round(summary.total_tokens / summary.request_count) : 0} tokens/request
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
