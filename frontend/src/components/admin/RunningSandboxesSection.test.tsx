import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RunningSandboxesSection } from './RunningSandboxesSection';
import * as adminApi from '@/services/adminApi';

vi.mock('@/services/adminApi');
const mockedApi = vi.mocked(adminApi);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RunningSandboxesSection', () => {
  it('lists running sandboxes with owner email, uptime and state', async () => {
    mockedApi.getActiveSandboxes.mockResolvedValue({
      count: 2,
      sandboxes: [
        {
          session_id: 'sid-1',
          user_id: 'u1',
          user_email: 'alice@x',
          status: 'Up 3 minutes',
          in_use: 2,
        },
        {
          session_id: 'sid-2',
          user_id: null,
          user_email: null,
          status: 'Up 1 hour',
          in_use: 0,
        },
      ],
    });
    render(<RunningSandboxesSection />);

    expect(await screen.findByText('alice@x')).toBeInTheDocument();
    expect(screen.getByText('(2)')).toBeInTheDocument();
    expect(screen.getByText(/active \(2\)/i)).toBeInTheDocument(); // in_use > 0
    expect(screen.getByText(/idle/i)).toBeInTheDocument(); // in_use == 0
  });

  it('shows an empty state when nothing is running', async () => {
    mockedApi.getActiveSandboxes.mockResolvedValue({ count: 0, sandboxes: [] });
    render(<RunningSandboxesSection />);
    expect(
      await screen.findByText(/no conversation sandboxes are currently running/i)
    ).toBeInTheDocument();
  });

  it('refreshes on demand', async () => {
    mockedApi.getActiveSandboxes.mockResolvedValue({ count: 0, sandboxes: [] });
    render(<RunningSandboxesSection />);
    await screen.findByText(/no conversation sandboxes/i);
    await userEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(mockedApi.getActiveSandboxes).toHaveBeenCalledTimes(2));
  });

  it('surfaces a load error', async () => {
    mockedApi.getActiveSandboxes.mockRejectedValue(new Error('boom'));
    render(<RunningSandboxesSection />);
    expect(await screen.findByText('boom')).toBeInTheDocument();
  });

  it('terminates a sandbox after an inline confirm and reloads', async () => {
    const one = {
      count: 1,
      sandboxes: [
        {
          session_id: 'sid-1',
          user_id: 'u1',
          user_email: 'alice@x',
          status: 'Up 3 minutes',
          in_use: 1,
        },
      ],
    };
    mockedApi.getActiveSandboxes
      .mockResolvedValueOnce(one) // initial load
      .mockResolvedValueOnce({ count: 0, sandboxes: [] }); // reload after kill
    mockedApi.terminateSandbox.mockResolvedValue(undefined);

    render(<RunningSandboxesSection />);
    await screen.findByText('alice@x');

    // Two-step: Terminate arms an inline Confirm (no window.confirm).
    await userEvent.click(screen.getByRole('button', { name: /^terminate$/i }));
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }));

    await waitFor(() => expect(mockedApi.terminateSandbox).toHaveBeenCalledWith('sid-1'));
    expect(
      await screen.findByText(/no conversation sandboxes are currently running/i)
    ).toBeInTheDocument();
  });

  it('cancels an armed termination without calling the API', async () => {
    mockedApi.getActiveSandboxes.mockResolvedValue({
      count: 1,
      sandboxes: [
        {
          session_id: 'sid-1',
          user_id: 'u1',
          user_email: 'alice@x',
          status: 'Up 3 minutes',
          in_use: 0,
        },
      ],
    });
    render(<RunningSandboxesSection />);
    await screen.findByText('alice@x');
    await userEvent.click(screen.getByRole('button', { name: /^terminate$/i }));
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(mockedApi.terminateSandbox).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /^terminate$/i })).toBeInTheDocument();
  });
});
