import { useState } from 'react';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableCell,
} from '@/components/ui';
import type { ModelUsage } from '@/types/admin';
import { cycleSort, type SortState } from '@/lib/tableSort';
import {
  formatTokens,
  formatTokenPair,
  formatCost,
  CACHE_LABEL,
  CACHE_TOOLTIP,
  IN_OUT_TOOLTIP,
} from '@/lib/usageFormat';
import { SortableHead } from './SortableHead';

type ModelSortKey =
  | 'model'
  | 'engine'
  | 'request_count'
  | 'input_tokens'
  | 'total_tokens'
  | 'cache_read_tokens'
  | 'cost_usd';

/**
 * "Usage by Model" — flat Model×Engine breakdown with tristate column sort. Identical on the
 * admin and self-service reports, so it lives here once. Owns its own sort state; the same
 * model can appear once per engine it ran on (rows are keyed by `model|engine`).
 */
export function ModelUsageTable({ data }: { data: ModelUsage[] }) {
  const [sort, setSort] = useState<SortState<ModelSortKey>>({ key: 'cost_usd', dir: 'desc' });
  const key = sort.key;
  const sorted =
    key === null
      ? data
      : [...data].sort((a, b) => {
          const dir = sort.dir === 'asc' ? 1 : -1;
          if (key === 'model' || key === 'engine') {
            return dir * (a[key] || '').localeCompare(b[key] || '');
          }
          return dir * (((a[key] as number) || 0) - ((b[key] as number) || 0));
        });
  const toggle = (k: ModelSortKey) => setSort((s) => cycleSort(s, k));

  return (
    <Card variant="bordered">
      <CardHeader>
        <CardTitle>Usage by Model</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <SortableHead label="Model" sortKey="model" sort={sort} onSort={toggle} />
              <SortableHead label="Engine" sortKey="engine" sort={sort} onSort={toggle} />
              <SortableHead label="Requests" sortKey="request_count" sort={sort} onSort={toggle} numeric />
              <SortableHead label="In / Out" sortKey="input_tokens" sort={sort} onSort={toggle} numeric title={IN_OUT_TOOLTIP} />
              <SortableHead label="Tokens" sortKey="total_tokens" sort={sort} onSort={toggle} numeric />
              <SortableHead label={CACHE_LABEL} sortKey="cache_read_tokens" sort={sort} onSort={toggle} numeric title={CACHE_TOOLTIP} />
              <SortableHead label="Cost" sortKey="cost_usd" sort={sort} onSort={toggle} numeric />
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((usage) => (
              <TableRow key={`${usage.model}|${usage.engine}`}>
                <TableCell className="font-medium">{usage.model}</TableCell>
                <TableCell className="text-gray-400">{usage.engine}</TableCell>
                <TableCell className="text-right">{usage.request_count}</TableCell>
                <TableCell className="text-right text-gray-400">
                  {formatTokenPair(usage.input_tokens, usage.output_tokens)}
                </TableCell>
                <TableCell className="text-right">{formatTokens(usage.total_tokens)}</TableCell>
                <TableCell className="text-right text-gray-400">
                  {formatTokenPair(usage.cache_read_tokens, usage.cache_write_tokens)}
                </TableCell>
                <TableCell className="text-right">{formatCost(usage.cost_usd)}</TableCell>
              </TableRow>
            ))}
            {sorted.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8 text-gray-400">
                  No usage data for this period
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
