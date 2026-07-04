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
  TableHead,
  TableCell,
} from '@/components/ui';
import type { ModelUsage } from '@/types/admin';
import { cycleSort, sortArrow, ariaSort, type SortState } from '@/lib/tableSort';
import { formatTokens, formatCost } from '@/lib/usageFormat';

type ModelSortKey =
  | 'model'
  | 'engine'
  | 'request_count'
  | 'total_tokens'
  | 'cache_read_tokens'
  | 'cache_write_tokens'
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
          return dir * (((a[key] as number) || 0) - (((b[key] as number) || 0)));
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
              <TableHead className="cursor-pointer select-none" aria-sort={ariaSort(sort, 'model')} onClick={() => toggle('model')}>
                Model{sortArrow(sort, 'model')}
              </TableHead>
              <TableHead className="cursor-pointer select-none" aria-sort={ariaSort(sort, 'engine')} onClick={() => toggle('engine')}>
                Engine{sortArrow(sort, 'engine')}
              </TableHead>
              <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sort, 'request_count')} onClick={() => toggle('request_count')}>
                Requests{sortArrow(sort, 'request_count')}
              </TableHead>
              <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sort, 'total_tokens')} onClick={() => toggle('total_tokens')}>
                Tokens{sortArrow(sort, 'total_tokens')}
              </TableHead>
              <TableHead
                className="text-right cursor-pointer select-none"
                aria-sort={ariaSort(sort, 'cache_read_tokens')}
                onClick={() => toggle('cache_read_tokens')}
                title="Prompt-cache reads (billed ~0.1x input rate)"
              >
                Cache R{sortArrow(sort, 'cache_read_tokens')}
              </TableHead>
              <TableHead
                className="text-right cursor-pointer select-none"
                aria-sort={ariaSort(sort, 'cache_write_tokens')}
                onClick={() => toggle('cache_write_tokens')}
                title="Prompt-cache writes (billed ~1.25x input rate)"
              >
                Cache W{sortArrow(sort, 'cache_write_tokens')}
              </TableHead>
              <TableHead className="text-right cursor-pointer select-none" aria-sort={ariaSort(sort, 'cost_usd')} onClick={() => toggle('cost_usd')}>
                Cost{sortArrow(sort, 'cost_usd')}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((usage) => (
              <TableRow key={`${usage.model}|${usage.engine}`}>
                <TableCell className="font-medium">{usage.model}</TableCell>
                <TableCell className="text-gray-400">{usage.engine}</TableCell>
                <TableCell className="text-right">{usage.request_count}</TableCell>
                <TableCell className="text-right">{formatTokens(usage.total_tokens)}</TableCell>
                <TableCell className="text-right text-gray-400">{formatTokens(usage.cache_read_tokens || 0)}</TableCell>
                <TableCell className="text-right text-gray-400">{formatTokens(usage.cache_write_tokens || 0)}</TableCell>
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
