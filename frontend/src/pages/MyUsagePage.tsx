import { useEffect, useState } from 'react';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Select,
} from '@/components/ui';
import { useMyUsageStore, MY_USAGE_SESSIONS_LIMIT } from '@/store/usage';
import { TrendingUp, MessageSquare, Cpu, DollarSign } from 'lucide-react';
import type { UsageDateRange } from '@/services/usageApi';
import { cycleSort, pageWindow, type SortState } from '@/lib/tableSort';
import { formatTokens, formatCostTotal } from '@/lib/usageFormat';
import { ModelUsageTable } from '@/components/usage/ModelUsageTable';
import { TokenBreakdownCard } from '@/components/usage/TokenBreakdownCard';
import { UsageOverTimeChart } from '@/components/usage/UsageOverTimeChart';
import { SessionUsageTable } from '@/components/usage/SessionUsageTable';
import { sortSessions, type SessionSortKey } from '@/components/usage/sessionUsage';

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
  const sortedSessions = sortSessions(sessions, sessionSort);

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
                <SessionUsageTable
                  sessions={pagedSessions}
                  sort={sessionSort}
                  onToggleSort={toggleSessionSort}
                />
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
