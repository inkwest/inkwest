import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DashboardPage } from './DashboardPage';
import { useBudgetStore, useUsageStore, useUserStore } from '@/store/admin';
import * as adminApi from '@/services/adminApi';
import type { UsageSummary } from '@/types/admin';

vi.mock('@/services/adminApi');

function summary(overrides: Partial<UsageSummary>): UsageSummary {
  return {
    period: 'daily',
    period_start: '2026-11-23T00:00:00Z',
    total_input_tokens: 100,
    total_output_tokens: 1400,
    total_tokens: 1500,
    total_cost_usd: 1.25,
    request_count: 3,
    ...overrides,
  };
}

const DAILY = summary({});
const MONTHLY = summary({ period: 'monthly', total_tokens: 5_255_500, total_cost_usd: 937.3 });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(adminApi.getUsageSummary).mockResolvedValue(DAILY);
  vi.mocked(adminApi.getBudgets).mockResolvedValue([]);
  vi.mocked(adminApi.getUsers).mockResolvedValue([]);
  // Module-level zustand stores leak state between tests — reset what the page reads.
  useBudgetStore.setState({ budgets: [], isLoading: false, error: null });
  useUserStore.setState({ users: [], isLoading: false, error: null });
});

describe('DashboardPage', () => {
  it("shows today's usage even when Usage Reports left another period in the store", async () => {
    // Regression: the tiles used to read useUsageStore, i.e. whatever period
    // Usage Reports last selected (monthly by default), under a "Today's" label.
    useUsageStore.setState({ period: 'monthly', summary: MONTHLY });

    render(<DashboardPage />);

    expect(await screen.findByText('1.5K')).toBeInTheDocument();
    expect(adminApi.getUsageSummary).toHaveBeenCalledTimes(1);
    expect(adminApi.getUsageSummary).toHaveBeenCalledWith('daily');
    expect(screen.getByText('daily')).toBeInTheDocument();
    expect(screen.getAllByText('$1.25')).toHaveLength(2);
    expect(screen.queryByText('5255.5K')).not.toBeInTheDocument();
    expect(screen.queryByText('monthly')).not.toBeInTheDocument();
    expect(screen.getByText("Today's Tokens")).toHaveAttribute('title', 'Since 00:00 UTC');
    expect(screen.getByText("Today's Cost")).toHaveAttribute('title', 'Since 00:00 UTC');
  });

  it('reports a usage summary load failure instead of an empty day', async () => {
    vi.mocked(adminApi.getUsageSummary).mockRejectedValue(new Error('boom'));

    render(<DashboardPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load usage: boom');
    expect(screen.queryByText('No usage data available')).not.toBeInTheDocument();
    expect(screen.getAllByText('-')).toHaveLength(2);
  });

  it('counts active users and lists the first five with their status', async () => {
    const users = Array.from({ length: 6 }, (_, i) => ({
      id: `aaaaaaaa-0000-4000-8000-00000000000${i}`,
      email: `user${i}@example.com`,
      display_name: `User ${i}`,
      is_active: i !== 1,
    }));
    vi.mocked(adminApi.getUsers).mockResolvedValue(users as never);

    render(<DashboardPage />);

    expect(await screen.findByText('User 0')).toBeInTheDocument();
    expect(screen.getByText('user4@example.com')).toBeInTheDocument();
    expect(screen.queryByText('User 5')).not.toBeInTheDocument();
    expect(screen.getAllByText('Active')).toHaveLength(4);
    expect(screen.getAllByText('Inactive')).toHaveLength(1);
    // Tile counts all active users, not only the five listed.
    expect(screen.getByText('Active Users').nextElementSibling).toHaveTextContent('5');
  });

  it('says so when there are no users', async () => {
    render(<DashboardPage />);

    expect(await screen.findByText('No users found')).toBeInTheDocument();
  });
});
