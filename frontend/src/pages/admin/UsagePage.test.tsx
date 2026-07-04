import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { UsagePage } from './UsagePage';
import { useUsageStore } from '@/store/admin';

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
  total_cost_usd: 55.123456,
  request_count: 10,
};

const USER_USAGE = [
  {
    user_id: 'bbbbbbbb-0000-4000-8000-000000000001',
    email: 'heavy@example.com',
    display_name: 'Heavy User',
    request_count: 7,
    conversation_count: 2,
    input_tokens: 4000,
    output_tokens: 2500,
    cache_read_tokens: 6000,
    cache_write_tokens: 1500,
    total_tokens: 6500,
    cost_usd: 50,
  },
  {
    // Legacy aggregate without cache fields — must render 0, never NaN.
    user_id: 'bbbbbbbb-0000-4000-8000-000000000002',
    email: 'old@example.com',
    display_name: 'Legacy User',
    request_count: 3,
    conversation_count: 1,
    input_tokens: 1000,
    output_tokens: 500,
    total_tokens: 1500,
    cost_usd: 5,
  },
];

function renderPage() {
  useUsageStore.setState({
    summary: SUMMARY,
    userUsage: USER_USAGE as never,
    modelUsage: [],
    timeseries: [],
    period: 'monthly',
    dateRange: {},
    isLoading: false,
    error: null,
    fetchAll: vi.fn(async () => {}),
  } as never);
  return render(
    <MemoryRouter>
      <UsagePage />
    </MemoryRouter>,
  );
}

describe('admin UsagePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the headline cost with exactly 2 decimals', () => {
    renderPage();
    expect(screen.getByText('$55.12')).toBeInTheDocument();
  });

  it('renders the token breakdown cache tiles', () => {
    renderPage();
    expect(screen.getByText('Cache Read')).toBeInTheDocument();
    expect(screen.getByText('Cache Write')).toBeInTheDocument();
    expect(screen.getByText('7.8k')).toBeInTheDocument();
  });

  it('renders cache columns in the per-user table, 0 for legacy rows', () => {
    renderPage();
    const legacyRow = screen.getByText('Legacy User').closest('tr')!;
    const cells = within(legacyRow).getAllByRole('cell').map((c) => c.textContent);
    // chevron, user, convs, req/conv, tokens, cache R, cache W, cost, budget button
    expect(cells.slice(2, 8)).toEqual(['1', '3.0', '1.5k', '0', '0', '$5.00']);
  });
});
