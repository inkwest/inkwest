import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { MyUsagePage } from './MyUsagePage';
import { useMyUsageStore } from '@/store/usage';

// recharts needs real layout measurements; the chart internals are not under test.
vi.mock('recharts', async (importOriginal) => {
  const mod = await importOriginal<typeof import('recharts')>();
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  };
});

const SUMMARY = {
  period: 'monthly',
  period_start: '2026-07-01T00:00:00Z',
  total_input_tokens: 5000,
  total_output_tokens: 3000,
  total_cache_read_tokens: 7777,
  total_cache_write_tokens: 2000,
  total_tokens: 8000,
  total_cost_usd: 1.239,
  request_count: 10,
};

const SESSIONS = [
  {
    session_id: 'aaaaaaaa-0000-4000-8000-000000000001',
    title: 'Costly conv',
    input_tokens: 4000,
    output_tokens: 2500,
    cache_read_tokens: 6000,
    cache_write_tokens: 1500,
    total_tokens: 6500,
    cost_usd: 1.0,
    request_count: 7,
    first_request: null,
    last_request: null,
  },
  {
    // Legacy row: no cache fields at all — must render 0 and sort deterministically.
    session_id: 'aaaaaaaa-0000-4000-8000-000000000002',
    title: 'Legacy conv',
    input_tokens: 1000,
    output_tokens: 500,
    total_tokens: 1500,
    cost_usd: 0.2,
    request_count: 3,
    first_request: null,
    last_request: null,
  },
];

function renderPage() {
  useMyUsageStore.setState({
    summary: SUMMARY,
    sessions: SESSIONS as never,
    modelUsage: [],
    timeseries: [],
    isLoading: false,
    error: null,
    fetchAll: vi.fn(async () => {}),
  } as never);
  return render(
    <MemoryRouter>
      <MyUsagePage />
    </MemoryRouter>,
  );
}

describe('MyUsagePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the summary tiles with a 2-decimal headline cost', () => {
    renderPage();
    expect(screen.getByText('Total Cost')).toBeInTheDocument();
    expect(screen.getByText('$1.24')).toBeInTheDocument(); // formatCostTotal, never 4 decimals
  });

  it('renders the token breakdown with cache tiles', () => {
    renderPage();
    expect(screen.getByText('Cache Read')).toBeInTheDocument();
    expect(screen.getByText('Cache Write')).toBeInTheDocument();
    expect(screen.getByText('7.8k')).toBeInTheDocument(); // 7777 cache read
  });

  it('renders cache columns in the conversations table, 0 for legacy rows', () => {
    renderPage();
    const legacyRow = screen.getByText('Legacy conv').closest('tr')!;
    const cells = within(legacyRow).getAllByRole('cell').map((c) => c.textContent);
    // title, requests, tokens, cache R, cache W, cost
    expect(cells).toEqual(['Legacy conv', '3', '1.5k', '0', '0', '$0.20']);
  });

  it('sorts by a cache column with legacy-undefined rows coerced to 0', () => {
    renderPage();
    // Two tables carry a Cache R column (conversations + by-model); the conversations
    // table renders first in the DOM.
    fireEvent.click(screen.getAllByRole('columnheader', { name: /^Cache R/ })[0]);
    const rows = screen.getAllByRole('row');
    const titles = rows
      .map((r) => within(r).queryAllByRole('cell')[0]?.textContent)
      .filter((t) => t === 'Costly conv' || t === 'Legacy conv');
    expect(titles).toEqual(['Legacy conv', 'Costly conv']); // asc: undefined-as-0 first
  });
});
