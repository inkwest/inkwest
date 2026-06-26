import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  Select,
} from '@/components/ui';
import { useMyUsageStore, MY_USAGE_SESSIONS_LIMIT } from '@/store/usage';
import { TrendingUp, MessageSquare, Cpu, DollarSign } from 'lucide-react';
import type { UsageDateRange } from '@/services/usageApi';
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

const periodOptions = [
  { value: 'hourly', label: 'Last Hour' },
  { value: 'daily', label: 'Today' },
  { value: 'weekly', label: 'This Week' },
  { value: 'monthly', label: 'This Month' },
  { value: 'last_month', label: 'Last Month' },
  { value: 'custom', label: 'Custom Range' },
];

function lastMonthRange(): UsageDateRange {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const end = new Date(now.getFullYear(), now.getMonth(), 0);
  return {
    startDate: start.toISOString().slice(0, 10),
    endDate: end.toISOString().slice(0, 10),
  };
}

function formatTokens(tokens: number) {
  if (tokens >= 1_000_000) return (tokens / 1_000_000).toFixed(2) + 'M';
  if (tokens >= 1_000) return (tokens / 1_000).toFixed(1) + 'K';
  return tokens.toString();
}

function formatCost(cost: number | string) {
  const n = typeof cost === 'string' ? parseFloat(cost) : cost;
  return n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

function formatTimeseriesLabel(iso: string, period: string) {
  const d = new Date(iso);
  if (period === 'hourly' || period === 'daily') return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toISOString().slice(0, 10);
}

export function MyUsagePage() {
  const {
    summary,
    sessions,
    modelUsage,
    timeseries,
    period,
    dateRange,
    isLoading,
    error,
    fetchAll,
    setPeriod,
    setDateRange,
  } = useMyUsageStore();

  const [customStart, setCustomStart] = useState(dateRange.startDate || '');
  const [customEnd, setCustomEnd] = useState(dateRange.endDate || '');

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const handlePeriodChange = (newPeriod: string) => {
    if (newPeriod === 'last_month') {
      setPeriod('last_month');
      setDateRange(lastMonthRange());
      fetchAll();
    } else if (newPeriod === 'custom') {
      setPeriod('custom');
    } else {
      setPeriod(newPeriod);
      setDateRange({});
      fetchAll();
    }
  };

  const handleCustomApply = () => {
    if (!customStart) return;
    setDateRange({ startDate: customStart, endDate: customEnd || undefined });
    fetchAll();
  };

  return (
    // main is overflow-hidden (chat scrolls internally) — own our scroll here.
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto w-full p-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold">My Usage</h1>
          <p className="text-gray-400 mt-1">Your conversations, tokens and costs</p>
        </div>
        <div className="w-48">
          <Select
            options={periodOptions}
            value={period}
            onChange={(e) => handlePeriodChange(e.target.value)}
          />
        </div>
      </div>

      {period === 'custom' && (
        <div className="flex items-end gap-3 flex-wrap">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Start date</label>
            <input
              type="date"
              value={customStart}
              onChange={(e) => setCustomStart(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded-md px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">End date</label>
            <input
              type="date"
              value={customEnd}
              onChange={(e) => setCustomEnd(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded-md px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <button
            onClick={handleCustomApply}
            disabled={!customStart}
            className="px-4 py-1.5 bg-blue-600 text-sm rounded-md hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Apply
          </button>
        </div>
      )}

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {error}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
        </div>
      ) : (
        <>
          {summary && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              <Card variant="bordered">
                <CardContent className="flex items-center gap-4">
                  <div className="p-3 rounded-lg bg-blue-900/20">
                    <TrendingUp className="w-6 h-6 text-blue-400" />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Total Requests</p>
                    <p className="text-2xl font-bold">{summary.request_count}</p>
                  </div>
                </CardContent>
              </Card>

              <Card variant="bordered">
                <CardContent className="flex items-center gap-4">
                  <div className="p-3 rounded-lg bg-purple-900/20">
                    <Cpu className="w-6 h-6 text-purple-400" />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Total Tokens</p>
                    <p className="text-2xl font-bold">{formatTokens(summary.total_tokens)}</p>
                  </div>
                </CardContent>
              </Card>

              <Card variant="bordered">
                <CardContent className="flex items-center gap-4">
                  <div className="p-3 rounded-lg bg-green-900/20">
                    <DollarSign className="w-6 h-6 text-green-400" />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Total Cost</p>
                    <p className="text-2xl font-bold">{formatCost(summary.total_cost_usd)}</p>
                  </div>
                </CardContent>
              </Card>

              <Card variant="bordered">
                <CardContent className="flex items-center gap-4">
                  <div className="p-3 rounded-lg bg-yellow-900/20">
                    <MessageSquare className="w-6 h-6 text-yellow-400" />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Conversations</p>
                    <p className="text-2xl font-bold">{sessions.length}</p>
                  </div>
                </CardContent>
              </Card>
            </div>
          )}

          {timeseries.length > 0 && (
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
                          ? [formatCost(Number(value) || 0), 'Cost']
                          : [formatTokens(Number(value) || 0), name === 'input_tokens' ? 'Input' : 'Output']
                      }
                    />
                    <Legend formatter={(value) => value === 'input_tokens' ? 'Input tokens' : value === 'output_tokens' ? 'Output tokens' : 'Cost'} />
                    <Bar yAxisId="tokens" dataKey="input_tokens" stackId="tokens" fill="#3B82F6" radius={[0, 0, 0, 0]} />
                    <Bar yAxisId="tokens" dataKey="output_tokens" stackId="tokens" fill="#10B981" radius={[4, 4, 0, 0]} />
                    <Line yAxisId="cost" type="monotone" dataKey="cost_usd" stroke="#F59E0B" strokeWidth={2} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* My Conversations */}
            <Card variant="bordered">
              <CardHeader>
                <CardTitle>My Conversations</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Conversation</TableHead>
                      <TableHead className="text-right">Requests</TableHead>
                      <TableHead className="text-right">Tokens</TableHead>
                      <TableHead className="text-right">Cost</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sessions.map((s) => (
                      <TableRow key={s.session_id}>
                        <TableCell>
                          <Link
                            to={`/session/${s.session_id}`}
                            className="text-blue-400 hover:text-blue-300 hover:underline"
                          >
                            {s.title}
                          </Link>
                        </TableCell>
                        <TableCell className="text-right text-gray-400">{s.request_count}</TableCell>
                        <TableCell className="text-right">{formatTokens(s.total_tokens)}</TableCell>
                        <TableCell className="text-right">{formatCost(s.cost_usd)}</TableCell>
                      </TableRow>
                    ))}
                    {sessions.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center py-8 text-gray-400">
                          No usage data for this period
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
                {/* Capped at the costliest N — flag truncation so the totals above
                    (which cover everything) aren't misread as this list's sum. */}
                {sessions.length >= MY_USAGE_SESSIONS_LIMIT && (
                  <p className="text-xs text-gray-500 px-4 py-2">
                    Showing the {MY_USAGE_SESSIONS_LIMIT} costliest conversations.
                  </p>
                )}
              </CardContent>
            </Card>

            {/* Usage by Model */}
            <Card variant="bordered">
              <CardHeader>
                <CardTitle>Usage by Model</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead className="text-right">Requests</TableHead>
                      <TableHead className="text-right">Tokens</TableHead>
                      <TableHead className="text-right">Cost</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {modelUsage.map((usage) => (
                      <TableRow key={usage.model}>
                        <TableCell className="font-medium">{usage.model}</TableCell>
                        <TableCell className="text-right">{usage.request_count}</TableCell>
                        <TableCell className="text-right">{formatTokens(usage.total_tokens)}</TableCell>
                        <TableCell className="text-right">{formatCost(usage.cost_usd)}</TableCell>
                      </TableRow>
                    ))}
                    {modelUsage.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center py-8 text-gray-400">
                          No usage data for this period
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </div>

          {summary && (
            <Card variant="bordered">
              <CardHeader>
                <CardTitle>Token Breakdown</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
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
                    <p className="text-sm text-gray-400">Requests</p>
                    <p className="text-xl font-bold">{summary.request_count}</p>
                    <p className="text-sm text-gray-500 mt-2">
                      ~{summary.request_count > 0 ? Math.round(summary.total_tokens / summary.request_count) : 0} tokens/request
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}
      </div>
    </div>
  );
}
