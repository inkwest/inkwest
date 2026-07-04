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
import { cycleSort, sortArrow, ariaSort, pageWindow, type SortState } from '@/lib/tableSort';
import { formatTokens, formatCost, TOKEN_SERIES_LABELS } from '@/lib/usageFormat';
import { ModelUsageTable } from '@/components/usage/ModelUsageTable';
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

function lastMonthRange(): adminApi.UsageDateRange {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const end = new Date(now.getFullYear(), now.getMonth(), 0);
  return {
    startDate: start.toISOString().slice(0, 10),
    endDate: end.toISOString().slice(0, 10),
  };
}

type UserSortKey =
  | 'display_name'
  | 'conversation_count'
  | 'mean_req'
  | 'cache_read_tokens'
  | 'cache_write_tokens'
  | 'total_tokens'
  | 'cost_usd';

// Average requests (turns) per conversation; 0 conversations ⇒ 0 (avoid divide-by-zero).
function meanRequestsPerConv(u: { request_count: number; conversation_count: number }) {
  return u.conversation_count > 0 ? u.request_count / u.conversation_count : 0;
}

const USERS_PAGE_SIZE = 10;

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

  // Client-side tristate sort for the "Usage by User" table (rows are already capped server-side).
  const [userSort, setUserSort] = useState<SortState<UserSortKey>>({ key: 'cost_usd', dir: 'desc' });
  const userKey = userSort.key;
  const sortedUserUsage =
    userKey === null
      ? userUsage
      : [...userUsage].sort((a, b) => {
          const dir = userSort.dir === 'asc' ? 1 : -1;
          if (userKey === 'display_name') {
            return dir * (a.display_name || a.email).localeCompare(b.display_name || b.email);
          }
          const val = (u: typeof a) =>
            userKey === 'mean_req' ? meanRequestsPerConv(u) : ((u[userKey] as number) || 0);
          return dir * (val(a) - val(b));
        });
  const toggleUserSort = (key: UserSortKey) => {
    setUserSort((s) => cycleSort(s, key));
    setUserPage(0);
  };

  // Front-only pager over the (server-capped, top-1000-by-cost) user rows.
  const [userPage, setUserPage] = useState(0);
  const {
    pageCount: userPageCount,
    clamped: userPageClamped,
    start: userStart,
    end: userEnd,
  } = pageWindow(sortedUserUsage.length, USERS_PAGE_SIZE, userPage);
  const pagedUserUsage = sortedUserUsage.slice(userStart, userEnd);

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
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Usage by User — clickable drill-down */}
            <Card variant="bordered">
              <CardHeader>
                {/* Pager lives in the header, not a footer: user rows vary in height (2-line
                    name+email), so a bottom pager shifts vertically between pages and the button
                    slips out from under the cursor. A fixed top anchor never moves. */}
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <CardTitle>Usage by User</CardTitle>
                  {sortedUserUsage.length > 0 && (
                    <div className="flex items-center gap-3 text-sm font-normal text-gray-400">
                      <span>
                        {userPageClamped * USERS_PAGE_SIZE + 1}–
                        {Math.min((userPageClamped + 1) * USERS_PAGE_SIZE, sortedUserUsage.length)} of{' '}
                        {sortedUserUsage.length}
                        <span className="text-gray-600"> · top 1000 by cost</span>
                      </span>
                      {userPageCount > 1 && (
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => setUserPage((p) => Math.max(0, p - 1))}
                            disabled={userPageClamped === 0}
                            className="px-2 py-1 rounded hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            ‹ Prev
                          </button>
                          <span>
                            {userPageClamped + 1} / {userPageCount}
                          </span>
                          <button
                            onClick={() => setUserPage((p) => Math.min(userPageCount - 1, p + 1))}
                            disabled={userPageClamped >= userPageCount - 1}
                            className="px-2 py-1 rounded hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            Next ›
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead></TableHead>
                      <TableHead className="cursor-pointer select-none" aria-sort={ariaSort(userSort, 'display_name')} onClick={() => toggleUserSort('display_name')}>
                        User{sortArrow(userSort, 'display_name')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'conversation_count')} onClick={() => toggleUserSort('conversation_count')}>
                        Convs{sortArrow(userSort, 'conversation_count')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'mean_req')} onClick={() => toggleUserSort('mean_req')}>
                        Req/conv{sortArrow(userSort, 'mean_req')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'total_tokens')} onClick={() => toggleUserSort('total_tokens')}>
                        Tokens{sortArrow(userSort, 'total_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'cache_read_tokens')} onClick={() => toggleUserSort('cache_read_tokens')} title="Prompt-cache reads (billed ~0.1x input rate)">
                        Cache R{sortArrow(userSort, 'cache_read_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'cache_write_tokens')} onClick={() => toggleUserSort('cache_write_tokens')} title="Prompt-cache writes (billed ~1.25x input rate; 2x for 1h TTL)">
                        Cache W{sortArrow(userSort, 'cache_write_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(userSort, 'cost_usd')} onClick={() => toggleUserSort('cost_usd')}>
                        Cost{sortArrow(userSort, 'cost_usd')}
                      </TableHead>
                      <TableHead className="w-8"></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedUserUsage.map((usage) => (
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
                            {usage.conversation_count}
                          </TableCell>
                          <TableCell className="text-right">
                            {meanRequestsPerConv(usage).toFixed(1)}
                          </TableCell>
                          <TableCell className="text-right">
                            {formatTokens(usage.total_tokens)}
                          </TableCell>
                          <TableCell className="text-right text-gray-400">
                            {formatTokens(usage.cache_read_tokens || 0)}
                          </TableCell>
                          <TableCell className="text-right text-gray-400">
                            {formatTokens(usage.cache_write_tokens || 0)}
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
                            <TableCell colSpan={9} className="bg-gray-900/50 p-0">
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
                        <TableCell colSpan={9} className="text-center py-8 text-gray-400">
                          No usage data for this period
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            {/* Usage by Model */}
            <ModelUsageTable data={modelUsage} />
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
