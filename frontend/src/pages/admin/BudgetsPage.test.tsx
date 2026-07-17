import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { BudgetsPage } from './BudgetsPage';
import { useBudgetStore, useUserStore } from '@/store/admin';
import * as adminApi from '@/services/adminApi';
import type { Budget } from '@/types/admin';

vi.mock('@/services/adminApi');

const USER = {
  id: 'aaaaaaaa-0000-4000-8000-000000000001',
  email: 'alice@example.com',
  display_name: 'Alice',
};

const NO_CAP_BUDGET: Budget = {
  id: 'bbbbbbbb-0000-4000-8000-000000000001',
  name: 'Alice',
  description: null,
  budget_type: 'user',
  target_id: USER.id,
  target_role: null,
  max_total_tokens: null,
  max_input_tokens: null,
  max_output_tokens: null,
  max_cost_usd: null,
  period: 'monthly',
  soft_limit_percent: 80,
  hard_limit_action: 'block',
  is_active: true,
  created_at: '2026-07-17T00:00:00Z',
  updated_at: '2026-07-17T00:00:00Z',
};

const NOTICE = /no caps set/i;

beforeEach(() => {
  vi.clearAllMocks();
  // The page mounts BudgetSettingsPanel + fetches budgets/users/usage — all mocked.
  vi.mocked(adminApi.getBudgetSettings).mockResolvedValue({
    enabled: true,
    default_monthly_cost_limit_usd: 20,
    default_enabled: false,
    default_default_monthly_cost_limit_usd: 0,
  });
  vi.mocked(adminApi.getBudgets).mockResolvedValue([]);
  vi.mocked(adminApi.getBudgetUsage).mockResolvedValue([]);
  vi.mocked(adminApi.getUsers).mockResolvedValue([USER as never]);
  vi.mocked(adminApi.createBudget).mockResolvedValue(NO_CAP_BUDGET);
  // Module-level zustand stores leak state between tests — reset what the page reads.
  useBudgetStore.setState({ budgets: [], isLoading: false, error: null });
  useUserStore.setState({ users: [] });
});

async function openCreateModal() {
  render(
    <MemoryRouter>
      <BudgetsPage />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: /add budget/i }));
  // Users load async; the User select must be populated before we pick one.
  await screen.findByRole('option', { name: 'Alice' });
}

describe('BudgetsPage create modal', () => {
  it('creates a no-cap budget: notice shown, both caps submitted as undefined', async () => {
    await openCreateModal();

    // Both caps empty by default — the exemption notice replaces the old hard block.
    expect(screen.getByText(NOTICE)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('User'), USER.id);
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() =>
      expect(adminApi.createBudget).toHaveBeenCalledWith(
        expect.objectContaining({
          budget_type: 'user',
          target_id: USER.id,
          max_cost_usd: undefined,
          max_total_tokens: undefined,
        }),
      ),
    );
    // Modal closed — the submit was accepted, not blocked by client validation.
    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument());
  });

  it('hides the no-cap notice as soon as a cap is set, and shows it again when cleared', async () => {
    await openCreateModal();
    const costCap = screen.getByLabelText('Cost cap (USD)');

    await userEvent.type(costCap, '25');
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();

    await userEvent.clear(costCap);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
  });
});
