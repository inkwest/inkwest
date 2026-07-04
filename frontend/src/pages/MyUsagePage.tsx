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
import { cycleSort, sortArrow, ariaSort, pageWindow, type SortState } from '@/lib/tableSort';
import { formatTokens, formatCost, formatCostTotal } from '@/lib/usageFormat';
import { ModelUsageTable } from '@/components/usage/ModelUsageTable';
import { TokenBreakdownCard } from '@/components/usage/TokenBreakdownCard';
import { UsageOverTimeChart } from '@/components/usage/UsageOverTimeChart';

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

type SessionSortKey =
  | 'title'
  | 'request_count'
  | 'total_tokens'
  | 'cache_read_tokens'
  | 'cache_write_tokens'
  | 'cost_usd';

const SESSIONS_PAGE_SIZE = 10;

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

  // Client-side tristate sort over the (server-capped, costliest-first) conversation rows.
  const [sessionSort, setSessionSort] = useState<SortState<SessionSortKey>>({ key: 'cost_usd', dir: 'desc' });
  const sessionKey = sessionSort.key;
  const sortedSessions =
    sessionKey === null
      ? sessions
      : [...sessions].sort((a, b) => {
          const dir = sessionSort.dir === 'asc' ? 1 : -1;
          if (sessionKey === 'title') {
            return dir * (a.title || '').localeCompare(b.title || '');
          }
          return dir * (((a[sessionKey] as number) || 0) - ((b[sessionKey] as number) || 0));
        });

  // Front-only pager over the conversation rows.
  const [sessionPage, setSessionPage] = useState(0);
  const {
    pageCount: sessionPageCount,
    clamped: sessionPageClamped,
    start: sessionStart,
    end: sessionEnd,
  } = pageWindow(sortedSessions.length, SESSIONS_PAGE_SIZE, sessionPage);
  const pagedSessions = sortedSessions.slice(sessionStart, sessionEnd);
  const toggleSessionSort = (key: SessionSortKey) => {
    setSessionSort((s) => cycleSort(s, key));
    setSessionPage(0);
  };

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
      <div className="w-full p-6 space-y-6">
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
                    <p className="text-2xl font-bold">{formatCostTotal(summary.total_cost_usd)}</p>
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

          <UsageOverTimeChart timeseries={timeseries} period={period} />

          {summary && <TokenBreakdownCard summary={summary} />}

          <div className="space-y-6">
            {/* My Conversations */}
            <Card variant="bordered">
              <CardHeader>
                {/* Pager lives in the header, not a footer: row heights vary (wrapped titles),
                    so a bottom pager shifts vertically between pages and the button slips out
                    from under the cursor. A fixed top anchor never moves. */}
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <CardTitle>My Conversations</CardTitle>
                  {sortedSessions.length > 0 && (
                    <div className="flex items-center gap-3 text-sm font-normal text-gray-400">
                      <span>
                        {sessionPageClamped * SESSIONS_PAGE_SIZE + 1}–
                        {Math.min((sessionPageClamped + 1) * SESSIONS_PAGE_SIZE, sortedSessions.length)} of{' '}
                        {sortedSessions.length}
                        {/* Server caps the list at the costliest N — flag truncation so the totals
                            above (which cover everything) aren't misread as this list's sum. */}
                        {sessions.length >= MY_USAGE_SESSIONS_LIMIT && (
                          <span className="text-gray-600"> · top {MY_USAGE_SESSIONS_LIMIT} by cost</span>
                        )}
                      </span>
                      {sessionPageCount > 1 && (
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => setSessionPage((p) => Math.max(0, p - 1))}
                            disabled={sessionPageClamped === 0}
                            className="px-2 py-1 rounded hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            ‹ Prev
                          </button>
                          <span>
                            {sessionPageClamped + 1} / {sessionPageCount}
                          </span>
                          <button
                            onClick={() => setSessionPage((p) => Math.min(sessionPageCount - 1, p + 1))}
                            disabled={sessionPageClamped >= sessionPageCount - 1}
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
                      <TableHead className="cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'title')} onClick={() => toggleSessionSort('title')}>
                        Conversation{sortArrow(sessionSort, 'title')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'request_count')} onClick={() => toggleSessionSort('request_count')}>
                        Requests{sortArrow(sessionSort, 'request_count')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'total_tokens')} onClick={() => toggleSessionSort('total_tokens')}>
                        Tokens{sortArrow(sessionSort, 'total_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'cache_read_tokens')} onClick={() => toggleSessionSort('cache_read_tokens')} title="Prompt-cache reads (billed ~0.1x input rate)">
                        Cache R{sortArrow(sessionSort, 'cache_read_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'cache_write_tokens')} onClick={() => toggleSessionSort('cache_write_tokens')} title="Prompt-cache writes (billed ~1.25x input rate; 2x for 1h TTL)">
                        Cache W{sortArrow(sessionSort, 'cache_write_tokens')}
                      </TableHead>
                      <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sessionSort, 'cost_usd')} onClick={() => toggleSessionSort('cost_usd')}>
                        Cost{sortArrow(sessionSort, 'cost_usd')}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedSessions.map((s) => (
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
                        <TableCell className="text-right text-gray-400">{formatTokens(s.cache_read_tokens || 0)}</TableCell>
                        <TableCell className="text-right text-gray-400">{formatTokens(s.cache_write_tokens || 0)}</TableCell>
                        <TableCell className="text-right">{formatCost(s.cost_usd)}</TableCell>
                      </TableRow>
                    ))}
                    {sessions.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={6} className="text-center py-8 text-gray-400">
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

        </>
      )}
      </div>
    </div>
  );
}
