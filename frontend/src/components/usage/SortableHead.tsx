import { clsx } from 'clsx';
import { TableHead } from '@/components/ui';
import { sortArrow, ariaSort, type SortState } from '@/lib/tableSort';

interface SortableHeadProps<K extends string> {
  label: string;
  sortKey: K;
  sort: SortState<K>;
  onSort: (key: K) => void;
  numeric?: boolean;
  title?: string;
  className?: string;
}

// The label is a <button> so the sort is reachable by keyboard; aria-sort stays on the <th>.
// Preflight resets a button's text-transform and alignment: inherit the header's.
export function SortableHead<K extends string>({
  label,
  sortKey,
  sort,
  onSort,
  numeric = false,
  title,
  className,
}: SortableHeadProps<K>) {
  return (
    <TableHead className={clsx(numeric && 'text-right', className)} aria-sort={ariaSort(sort, sortKey)}>
      <button
        type="button"
        className="w-full cursor-pointer select-none [text-align:inherit] [text-transform:inherit]"
        title={title}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span aria-hidden="true">{sortArrow(sort, sortKey)}</span>
      </button>
    </TableHead>
  );
}
