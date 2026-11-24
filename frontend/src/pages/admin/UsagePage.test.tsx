import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { UsagePage } from './UsagePage';
import { useUsageStore } from '@/store/admin';
import * as adminApi from '@/services/adminApi';

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

function renderPage(userUsage: unknown[] = USER_USAGE) {
  useUsageStore.setState({
    summary: SUMMARY,
    userUsage: userUsage as never,
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

  it('renders the combined cache column in the per-user table, 0 for legacy rows', () => {
    renderPage();
    const legacyRow = screen.getByText('Legacy User').closest('tr')!;
    const cells = within(legacyRow).getAllByRole('cell').map((c) => c.textContent);
    // chevron, user, convs, req/conv, in / out, tokens, cache (read / write), cost, budget button
    expect(cells.slice(2, 8)).toEqual(['1', '3.0', '1.0k / 500', '1.5k', '0 / 0', '$5.00']);
  });

  it.each([
    [/^In \/ Out/, 'input'],
    [/^Cache R \/ W/, 'cache reads'],
  ])('sorts the user table on %s by its first figure (%s)', (name) => {
    // The two figures of a pair order the users differently, so the order shows which one sorts.
    const user = (id: string, a: number, b: number) => ({
      ...USER_USAGE[0],
      user_id: id,
      display_name: id,
      input_tokens: a,
      output_tokens: b,
      cache_read_tokens: a,
      cache_write_tokens: b,
    });
    renderPage([user('B-user', 900, 10), user('A-user', 10, 900)]);
    fireEvent.click(screen.getAllByRole('button', { name })[0]);
    const names = screen
      .getAllByRole('row')
      .map((r) => r.textContent ?? '')
      .filter((t) => t.includes('A-user') || t.includes('B-user'))
      .map((t) => (t.includes('A-user') ? 'A' : 'B'));
    expect(names).toEqual(['A', 'B']); // asc by the first figure: 10 before 900
  });

  describe('per-user session drilldown', () => {
    // Server order is costliest-first; "Small" has more tokens so a Tokens sort flips the order.
    const sessions = (ids: string[]) =>
      ids.map((id, i) => ({
        session_id: id,
        title: id,
        input_tokens: 10,
        output_tokens: 10,
        total_tokens: id === 'Small' ? 9000 : 100,
        cost_usd: 10 - i,
        request_count: 1,
        first_request: null,
        last_request: null,
      }));

    const drilldownTitles = () =>
      screen.getAllByRole('link').map((a) => a.textContent).filter((t) => t === 'Big' || t === 'Small');

    async function expand(user: string) {
      fireEvent.click(screen.getByText(user));
      await waitFor(() => expect(screen.getByRole('link', { name: 'Big' })).toBeInTheDocument());
    }

    it('sorts the drilldown, back to server order on the third click, and resets per user', async () => {
      const spy = vi
        .spyOn(adminApi, 'getUsageByUserSessions')
        .mockImplementation(async (userId) => ({
          user_id: userId,
          display_name: '',
          email: '',
          sessions: sessions(['Big', 'Small']),
        }));
      renderPage();
      await expand('Heavy User');
      expect(drilldownTitles()).toEqual(['Big', 'Small']);

      // The user and model tables have a Tokens header too: scope to the drilldown table.
      const tokens = () =>
        within(screen.getByRole('link', { name: 'Big' }).closest('table')!).getByRole('button', {
          name: /^Tokens/,
        });
      fireEvent.click(tokens());
      expect(drilldownTitles()).toEqual(['Big', 'Small']); // asc: 100 < 9000
      fireEvent.click(tokens());
      expect(drilldownTitles()).toEqual(['Small', 'Big']); // desc
      fireEvent.click(tokens());
      expect(drilldownTitles()).toEqual(['Big', 'Small']); // none: server order
      fireEvent.click(tokens());
      fireEvent.click(tokens()); // desc again, then switch user

      await expand('Legacy User');
      expect(spy).toHaveBeenCalledTimes(2);
      expect(drilldownTitles()).toEqual(['Big', 'Small']); // cost desc again for the new user
    });
  });
});
