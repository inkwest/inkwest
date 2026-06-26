import { Fragment, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
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
import { useUsageStore } from '@/store/admin';
import { TrendingUp, Users, Cpu, DollarSign, ChevronRight, ChevronDown, Wallet } from 'lucide-react';
import { UserSessionsUsage } from '@/types/admin';
import * as adminApi from '@/services/adminApi';
import {
  BarChart,
  Bar,
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

function lastMonthRange(): adminApi.UsageDateRange {
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

export function UsagePage() {
  const {
    summary,
    userUsage,
    modelUsage,
    timeseries,
    period,
    dateRange,
    isLoading,
    error,
    fetchAll,
    setPeriod,
    setDateRange,
  } = useUsageStore();

  const [expandedUser, setExpandedUser] = useState<string | null>(null);
  const [sessionsData, setSessionsData] = useState<UserSessionsUsage | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [customStart, setCustomStart] = useState(dateRange.startDate || '');
  const [customEnd, setCustomEnd] = useState(dateRange.endDate || '');
  // Right-click "create budget" menu, positioned at the cursor.
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; userId: string; name: string } | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Dismiss the context menu on any click, scroll, or Escape.
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setCtxMenu(null);
    window.addEventListener('click', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', onKey);
    };
  }, [ctxMenu]);

  // Jump to Budgets with the Create modal prefilled for this user (see BudgetsPage).
  const createBudgetFor = (userId: string) =>
    navigate('/admin/budgets', { state: { createForUserId: userId } });

  const handlePeriodChange = (newPeriod: string) => {
    setExpandedUser(null);
    setSessionsData(null);

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

  const handleUserClick = async (userId: string) => {
    if (expandedUser === userId) {
      setExpandedUser(null);
      setSessionsData(null);
      return;
    }
    setExpandedUser(userId);
    setSessionsLoading(true);
    const apiPeriod = period === 'last_month' || period === 'custom' ? 'daily' : period;
    try {
      const data = await adminApi.getUsageByUserSessions(userId, apiPeriod, dateRange);
      setSessionsData(data);
    } catch {
      setSessionsData(null);
    } finally {
      setSessionsLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold">Usage Reports</h1>
          <p className="text-gray-400 mt-1">Monitor token usage and costs</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-48">
            <Select
              options={periodOptions}
              value={period}
              onChange={(e) => handlePeriodChange(e.target.value)}
            />
          </div>
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
                    <Users className="w-6 h-6 text-yellow-400" />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Active Users</p>
                    <p className="text-2xl font-bold">{userUsage.length}</p>
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
                  <BarChart data={timeseries.map((p) => ({
                    ...p,
                    label: formatTimeseriesLabel(p.timestamp, period),
                    total_tokens: p.input_tokens + p.output_tokens,
                  }))}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                    <XAxis dataKey="label" tick={{ fill: '#9CA3AF', fontSize: 12 }} />
                    <YAxis tick={{ fill: '#9CA3AF', fontSize: 12 }} tickFormatter={(v) => formatTokens(v)} />
                    <Tooltip
                      contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: 8 }}
                      labelStyle={{ color: '#E5E7EB' }}
                      formatter={(value, name) => [formatTokens(Number(value) || 0), name === 'input_tokens' ? 'Input' : 'Output']}
                    />
                    <Legend formatter={(value) => value === 'input_tokens' ? 'Input tokens' : 'Output tokens'} />
                    <Bar dataKey="input_tokens" stackId="tokens" fill="#3B82F6" radius={[0, 0, 0, 0]} />
                    <Bar dataKey="output_tokens" stackId="tokens" fill="#10B981" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Usage by User — clickable drill-down */}
            <Card variant="bordered">
              <CardHeader>
                <CardTitle>Usage by User</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead></TableHead>
                      <TableHead>User</TableHead>
                      <TableHead className="text-right">Tokens</TableHead>
                      <TableHead className="text-right">Cost</TableHead>
                      <TableHead className="w-8"></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {userUsage.map((usage) => (
                      <Fragment key={usage.user_id}>
                        <TableRow
                          className="group cursor-pointer hover:bg-gray-700/50"
                          onClick={() => usage.user_id && handleUserClick(usage.user_id)}
                          onContextMenu={(e) => {
                            if (!usage.user_id) return;
                            e.preventDefault();
                            setCtxMenu({
                              x: e.clientX,
                              y: e.clientY,
                              userId: usage.user_id,
                              name: usage.display_name || usage.email,
                            });
                          }}
                        >
                          <TableCell className="w-8 pr-0">
                            {expandedUser === usage.user_id
                              ? <ChevronDown className="w-4 h-4 text-gray-400" />
                              : <ChevronRight className="w-4 h-4 text-gray-400" />}
                          </TableCell>
                          <TableCell>
                            <div>
                              <p className="font-medium">{usage.display_name}</p>
                              <p className="text-sm text-gray-400">{usage.email}</p>
                            </div>
                          </TableCell>
                          <TableCell className="text-right">
                            {formatTokens(usage.total_tokens)}
                          </TableCell>
                          <TableCell className="text-right">
                            {formatCost(usage.cost_usd)}
                          </TableCell>
                          <TableCell className="w-8 pr-3">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                if (usage.user_id) createBudgetFor(usage.user_id);
                              }}
                              title="Create budget for this user"
                              aria-label="Create budget for this user"
                              className="opacity-0 group-hover:opacity-100 transition-opacity text-gray-400 hover:text-blue-400"
                            >
                              <Wallet className="w-4 h-4" />
                            </button>
                          </TableCell>
                        </TableRow>
                        {expandedUser === usage.user_id && (
                          <TableRow key={`${usage.user_id}-sessions`}>
                            <TableCell colSpan={5} className="bg-gray-900/50 p-0">
                              {sessionsLoading ? (
                                <div className="flex items-center justify-center py-4">
                                  <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-500" />
                                </div>
                              ) : sessionsData?.sessions.length ? (
                                <table className="w-full text-sm">
                                  <thead>
                                    <tr className="text-gray-500 text-xs">
                                      <th className="text-left pl-12 py-2">Session</th>
                                      <th className="text-right py-2">Requests</th>
                                      <th className="text-right py-2">In / Out</th>
                                      <th className="text-right py-2 pr-4">Cost</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {sessionsData.sessions.map((s) => (
                                      <tr key={s.session_id} className="border-t border-gray-800">
                                        <td className="pl-12 py-2">
                                          <Link to={`/session/${s.session_id}`} className="text-blue-400 hover:text-blue-300 hover:underline">
                                            {s.title}
                                          </Link>
                                        </td>
                                        <td className="text-right py-2 text-gray-400">{s.request_count}</td>
                                        <td className="text-right py-2 text-gray-400">
                                          {formatTokens(s.input_tokens)} / {formatTokens(s.output_tokens)}
                                        </td>
                                        <td className="text-right py-2 pr-4 text-gray-300">
                                          {formatCost(s.cost_usd)}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              ) : (
                                <p className="text-center text-gray-500 py-4">No session data</p>
                              )}
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    ))}
                    {userUsage.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center py-8 text-gray-400">
                          No usage data for this period
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
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
                        <TableCell className="font-medium">
                          {usage.model}
                        </TableCell>
                        <TableCell className="text-right">
                          {usage.request_count}
                        </TableCell>
                        <TableCell className="text-right">
                          {formatTokens(usage.total_tokens)}
                        </TableCell>
                        <TableCell className="text-right">
                          {formatCost(usage.cost_usd)}
                        </TableCell>
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
                    <p className="text-xl font-bold">
                      {formatTokens(summary.total_input_tokens)}
                    </p>
                    <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-blue-500"
                        style={{
                          width: `${
                            summary.total_tokens > 0
                              ? (summary.total_input_tokens / summary.total_tokens) * 100
                              : 0
                          }%`,
                        }}
                      />
                    </div>
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Output Tokens</p>
                    <p className="text-xl font-bold">
                      {formatTokens(summary.total_output_tokens)}
                    </p>
                    <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-green-500"
                        style={{
                          width: `${
                            summary.total_tokens > 0
                              ? (summary.total_output_tokens / summary.total_tokens) * 100
                              : 0
                          }%`,
                        }}
                      />
                    </div>
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">Requests</p>
                    <p className="text-xl font-bold">{summary.request_count}</p>
                    <p className="text-sm text-gray-500 mt-2">
                      ~{summary.request_count > 0
                        ? Math.round(summary.total_tokens / summary.request_count)
                        : 0} tokens/request
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {ctxMenu && (
        <div
          className="fixed z-50 bg-gray-800 border border-gray-700 rounded-md shadow-lg py-1 text-sm"
          style={{ top: ctxMenu.y, left: ctxMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={() => {
              createBudgetFor(ctxMenu.userId);
              setCtxMenu(null);
            }}
            className="flex items-center gap-2 px-3 py-1.5 w-full text-left hover:bg-gray-700"
          >
            <Wallet className="w-4 h-4 text-gray-400" />
            Create budget for {ctxMenu.name}
          </button>
        </div>
      )}
    </div>
  );
}
