import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SandboxLimitsSection } from './SandboxLimitsSection';
import * as adminApi from '@/services/adminApi';

vi.mock('@/services/adminApi');
const mockedApi = vi.mocked(adminApi);

beforeEach(() => {
  vi.clearAllMocks();
  mockedApi.getSandboxSettings.mockResolvedValue({
    max_sessions_per_user: 5,
    default_max_sessions_per_user: 5,
    hard_ceiling: 7,
  });
});

describe('SandboxLimitsSection', () => {
  it('loads the current cap and reveals ceiling/default in the info tooltip on hover', async () => {
    render(<SandboxLimitsSection />);
    const input = await screen.findByLabelText(/max conversations per user/i);
    expect(input).toHaveValue(5);
    // Help text is hover-only now: hidden until the (i) icon is hovered, then it appears.
    expect(screen.queryByText(/deployment default: 5/i)).not.toBeInTheDocument();
    await userEvent.hover(screen.getByLabelText(/about this limit/i));
    expect(await screen.findByText(/deployment default: 5/i)).toBeInTheDocument();
    expect(screen.getByText(/bursts allowed up to 7 running/i)).toBeInTheDocument();
  });

  it('saves a new value and confirms', async () => {
    mockedApi.updateSandboxSettings.mockResolvedValue({
      max_sessions_per_user: 8,
      default_max_sessions_per_user: 5,
      hard_ceiling: 10,
    });
    render(<SandboxLimitsSection />);
    const input = await screen.findByLabelText(/max conversations per user/i);
    await userEvent.clear(input);
    await userEvent.type(input, '8');
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => expect(mockedApi.updateSandboxSettings).toHaveBeenCalledWith(8));
    expect(await screen.findByText(/effective immediately/i)).toBeInTheDocument();
  });

  it('blocks saving a negative value', async () => {
    render(<SandboxLimitsSection />);
    const input = await screen.findByLabelText(/max conversations per user/i);
    await userEvent.clear(input);
    await userEvent.type(input, '-1');

    expect(screen.getByText(/whole number ≥ 0/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
    expect(mockedApi.updateSandboxSettings).not.toHaveBeenCalled();
  });

  it('treats a cleared field as invalid, not as 0/unlimited', async () => {
    render(<SandboxLimitsSection />);
    const input = await screen.findByLabelText(/max conversations per user/i);
    await userEvent.clear(input);

    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(mockedApi.updateSandboxSettings).not.toHaveBeenCalled();
  });

  it('surfaces a save error', async () => {
    mockedApi.updateSandboxSettings.mockRejectedValue(new Error('boom'));
    render(<SandboxLimitsSection />);
    const input = await screen.findByLabelText(/max conversations per user/i);
    await userEvent.clear(input);
    await userEvent.type(input, '3');
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    expect(await screen.findByText('boom')).toBeInTheDocument();
  });
});
