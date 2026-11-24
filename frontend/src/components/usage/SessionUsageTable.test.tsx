import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { SessionUsageTable } from './SessionUsageTable';
import type { SessionUsage } from '@/types/admin';
import type { SortState } from '@/lib/tableSort';
import type { SessionSortKey } from './sessionUsage';
import { CACHE_TOOLTIP } from '@/lib/usageFormat';

const session: SessionUsage = {
  session_id: 's1',
  title: 'Debug pods',
  input_tokens: 1200,
  output_tokens: 300,
  cache_read_tokens: 5000,
  cache_write_tokens: 400,
  total_tokens: 1500,
  cost_usd: 0.42,
  request_count: 3,
  first_request: null,
  last_request: null,
};

function renderTable(
  sessions: SessionUsage[],
  sort: SortState<SessionSortKey> = { key: null, dir: 'desc' },
  onToggleSort = vi.fn(),
) {
  return render(
    <MemoryRouter>
      <SessionUsageTable sessions={sessions} sort={sort} onToggleSort={onToggleSort} />
    </MemoryRouter>,
  );
}

describe('SessionUsageTable', () => {
  it('spans the empty-state row across every column', () => {
    renderTable([]);
    const headers = screen.getAllByRole('columnheader');
    const empty = screen.getByText('No usage data for this period').closest('td');
    expect(empty).toHaveAttribute('colspan', String(headers.length));
  });

  it('renders one cell per column header', () => {
    renderTable([session]);
    const headers = screen.getAllByRole('columnheader');
    const row = screen.getByText('Debug pods').closest('tr');
    expect(row?.querySelectorAll('td')).toHaveLength(headers.length);
  });

  it('shows each pair in a fixed order: input / output, cache read / write', () => {
    renderTable([session]);
    const cells = within(screen.getByText('Debug pods').closest('tr')!).getAllByRole('cell');
    expect(cells.map((c) => c.textContent)).toEqual([
      'Debug pods',
      '3',
      '1.2k / 300',
      '1.5k',
      '5.0k / 400',
      '$0.42',
    ]);
  });

  it('marks only the active column with aria-sort', () => {
    renderTable([session], { key: 'cost_usd', dir: 'desc' });
    const sorted = screen
      .getAllByRole('columnheader')
      .filter((h) => h.getAttribute('aria-sort') !== 'none')
      .map((h) => [h.textContent?.trim(), h.getAttribute('aria-sort')]);
    expect(sorted).toEqual([['Cost\u00a0↓', 'descending']]);
  });

  it.each([
    [/^In \/ Out/, 'input_tokens'],
    [/^Cache R \/ W/, 'cache_read_tokens'],
  ])('sorts %s by its first figure (%s)', async (name, key) => {
    const onToggleSort = vi.fn();
    renderTable([session], undefined, onToggleSort);
    await userEvent.click(screen.getByRole('button', { name }));
    expect(onToggleSort).toHaveBeenCalledWith(key);
  });

  it('keeps the sort arrow out of the accessible name and the tooltip on the button', () => {
    renderTable([session], { key: 'cost_usd', dir: 'desc' });
    expect(screen.getByRole('button', { name: 'Cost' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Cache R \/ W/ })).toHaveAttribute('title', CACHE_TOOLTIP);
  });

  it('sorts from the keyboard', async () => {
    const onToggleSort = vi.fn();
    renderTable([session], undefined, onToggleSort);
    await userEvent.tab();
    expect(screen.getByRole('button', { name: /^Conversation/ })).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(onToggleSort).toHaveBeenCalledWith('title');
  });
});
