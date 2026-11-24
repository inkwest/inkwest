import type { ReactNode } from 'react';
import { clsx } from 'clsx';
import { Link } from 'react-router-dom';
import { Table, TableHeader, TableBody, TableRow, TableCell } from '@/components/ui';
import type { SortState } from '@/lib/tableSort';
import {
  formatTokens,
  formatTokenPair,
  formatCost,
  CACHE_LABEL,
  CACHE_TOOLTIP,
  IN_OUT_TOOLTIP,
} from '@/lib/usageFormat';
import type { SessionUsage } from '@/types/admin';
import type { SessionSortKey } from './sessionUsage';
import { SortableHead } from './SortableHead';

interface Column {
  key: SessionSortKey;
  label: string;
  title?: string;
  numeric?: boolean;
  muted?: boolean;
  cell: (s: SessionUsage) => ReactNode;
}

const COLUMNS: Column[] = [
  {
    key: 'title',
    label: 'Conversation',
    cell: (s) => (
      <Link to={`/session/${s.session_id}`} className="text-blue-400 hover:text-blue-300 hover:underline">
        {s.title}
      </Link>
    ),
  },
  { key: 'request_count', label: 'Requests', numeric: true, muted: true, cell: (s) => s.request_count },
  {
    key: 'input_tokens',
    label: 'In / Out',
    title: IN_OUT_TOOLTIP,
    numeric: true,
    muted: true,
    cell: (s) => formatTokenPair(s.input_tokens, s.output_tokens),
  },
  { key: 'total_tokens', label: 'Tokens', numeric: true, cell: (s) => formatTokens(s.total_tokens) },
  {
    key: 'cache_read_tokens',
    label: CACHE_LABEL,
    title: CACHE_TOOLTIP,
    numeric: true,
    muted: true,
    cell: (s) => formatTokenPair(s.cache_read_tokens, s.cache_write_tokens),
  },
  { key: 'cost_usd', label: 'Cost', numeric: true, cell: (s) => formatCost(s.cost_usd) },
];

interface SessionUsageTableProps {
  // Rows to render — the parent sorts (via sortSessions) and, for My Usage, pages first.
  sessions: SessionUsage[];
  sort: SortState<SessionSortKey>;
  onToggleSort: (key: SessionSortKey) => void;
  // Admin drilldown indents the first column under its parent user row.
  indent?: boolean;
}

export function SessionUsageTable({
  sessions,
  sort,
  onToggleSort,
  indent = false,
}: SessionUsageTableProps) {
  const firstCol = indent ? 'pl-12' : undefined;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((c, i) => (
            <SortableHead
              key={c.key}
              label={c.label}
              sortKey={c.key}
              sort={sort}
              onSort={onToggleSort}
              numeric={c.numeric}
              title={c.title}
              className={i === 0 ? firstCol : undefined}
            />
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {sessions.map((s) => (
          <TableRow key={s.session_id}>
            {COLUMNS.map((c, i) => (
              <TableCell
                key={c.key}
                className={clsx(c.numeric && 'text-right', c.muted && 'text-gray-400', i === 0 && firstCol)}
              >
                {c.cell(s)}
              </TableCell>
            ))}
          </TableRow>
        ))}
        {sessions.length === 0 && (
          <TableRow>
            <TableCell colSpan={COLUMNS.length} className="text-center py-8 text-gray-400">
              No usage data for this period
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
