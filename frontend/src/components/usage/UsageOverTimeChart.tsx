import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui';
import {
  formatTokens,
  formatCost,
  formatTimeseriesLabel,
  TOKEN_SERIES_LABELS,
} from '@/lib/usageFormat';
import type { UsageTimeseriesPoint } from '@/types/admin';
import {
  ComposedChart,
  Bar,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';

/**
 * The "Usage Over Time" composed chart (stacked token bars + cost line on its own axis).
 * One implementation for MyUsagePage and the admin UsagePage — series, colors and
 * formatters must evolve in lockstep on both. Renders nothing when the window is empty.
 */
export function UsageOverTimeChart({
  timeseries,
  period,
}: {
  timeseries: UsageTimeseriesPoint[];
  period: string;
}) {
  if (timeseries.length === 0) return null;
  return (
    <Card variant="bordered">
      <CardHeader>
        <CardTitle>Usage Over Time</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={timeseries.map((p) => ({
            ...p,
            label: formatTimeseriesLabel(p.timestamp, period),
            total_tokens: p.input_tokens + p.output_tokens,
          }))}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="label" tick={{ fill: '#9CA3AF', fontSize: 12 }} />
            {/* Left axis: token bars. Right axis: cost line — separate scales so a
                small dollar figure stays readable next to millions of tokens. */}
            <YAxis yAxisId="tokens" tick={{ fill: '#9CA3AF', fontSize: 12 }} tickFormatter={(v) => formatTokens(v)} />
            <YAxis yAxisId="cost" orientation="right" tick={{ fill: '#F59E0B', fontSize: 12 }} tickFormatter={(v) => formatCost(v)} />
            <Tooltip
              contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: 8 }}
              labelStyle={{ color: '#E5E7EB' }}
              formatter={(value, name) =>
                name === 'cost_usd'
                  ? [formatCost(Number(value) || 0), TOKEN_SERIES_LABELS.cost_usd]
                  : [formatTokens(Number(value) || 0), TOKEN_SERIES_LABELS[String(name)] ?? String(name)]
              }
            />
            <Legend formatter={(value) => TOKEN_SERIES_LABELS[String(value)] ?? String(value)} />
            {/* Cache bars stack separately: the primary bar keeps matching the
                "Total Tokens" figure (fresh+output, cache excluded), and cache-heavy
                turns don't crush the fresh/output bars into slivers. */}
            <Bar yAxisId="tokens" dataKey="input_tokens" stackId="tokens" fill="#3B82F6" radius={[0, 0, 0, 0]} />
            <Bar yAxisId="tokens" dataKey="output_tokens" stackId="tokens" fill="#10B981" radius={[4, 4, 0, 0]} />
            <Bar yAxisId="tokens" dataKey="cache_read_tokens" stackId="cache" fill="#8B5CF6" radius={[0, 0, 0, 0]} />
            <Bar yAxisId="tokens" dataKey="cache_write_tokens" stackId="cache" fill="#EC4899" radius={[4, 4, 0, 0]} />
            <Line yAxisId="cost" type="monotone" dataKey="cost_usd" stroke="#F59E0B" strokeWidth={2} dot={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
