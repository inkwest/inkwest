import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';

vi.mock('@/services/api', () => ({
  getAwsOptions: vi.fn(),
  refreshAwsOptions: vi.fn(),
}));

import { getAwsOptions, refreshAwsOptions } from '@/services/api';

const mockGet = vi.mocked(getAwsOptions);
const mockRefresh = vi.mocked(refreshAwsOptions);

const CACHED = {
  accounts: [{ account_id: '111', account_name: 'acme' }],
  roles: [{ account_id: '111', role_name: 'ReadOnly' }],
};
const FRESH = {
  accounts: [{ account_id: '111', account_name: 'acme' }],
  roles: [
    { account_id: '111', role_name: 'ReadOnly' },
    { account_id: '111', role_name: 'Admin' },
  ],
};

describe('AwsAccountRoleModal refresh', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads cached options on open, then re-enumerates on refresh click', async () => {
    mockGet.mockResolvedValue(CACHED);
    mockRefresh.mockResolvedValue(FRESH);

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={() => {}} />);

    // Cached list shown first — no portal hit.
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('s1'));
    expect(screen.getByText('ReadOnly')).toBeInTheDocument();
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();

    // Refresh button forces a fresh enumeration.
    fireEvent.click(screen.getByLabelText('Rafraîchir la liste des rôles'));
    await waitFor(() => expect(mockRefresh).toHaveBeenCalledWith('s1'));
    await waitFor(() => expect(screen.getByText('Admin')).toBeInTheDocument());
  });

  it('drops a checked role that vanished from the refreshed list', async () => {
    mockGet.mockResolvedValue(FRESH); // ReadOnly + Admin
    mockRefresh.mockResolvedValue(CACHED); // Admin gone after refresh

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Admin')).toBeInTheDocument());

    // Check both roles → counter shows 2.
    fireEvent.click(screen.getByText('Admin'));
    fireEvent.click(screen.getByText('ReadOnly'));
    expect(screen.getByText('2 sélectionné(s)')).toBeInTheDocument();

    // Refresh: Admin disappears → only ReadOnly stays selected.
    fireEvent.click(screen.getByLabelText('Rafraîchir la liste des rôles'));
    await waitFor(() => expect(screen.queryByText('Admin')).not.toBeInTheDocument());
    expect(screen.getByText('1 sélectionné(s)')).toBeInTheDocument();
  });

  it('does not call the API while closed', () => {
    render(<AwsAccountRoleModal sessionId="s1" isOpen={false} onClose={() => {}} />);
    expect(mockGet).not.toHaveBeenCalled();
  });
});
