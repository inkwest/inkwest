import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';

vi.mock('@/services/api', () => ({
  getAwsOptions: vi.fn(),
  refreshAwsOptions: vi.fn(),
  getAwsEnumProgress: vi.fn().mockResolvedValue({ phase: 'idle', done: 0, total: 0 }),
}));

// The grant goes through the store action; stub it so we can assert the flow.
const store = vi.hoisted(() => ({ selectAwsRoles: vi.fn() }));
vi.mock('@/store/connectors', () => ({
  useConnectorsStore: (selector?: (s: typeof store) => unknown) =>
    selector ? selector(store) : store,
}));

import { getAwsOptions, refreshAwsOptions, getAwsEnumProgress } from '@/services/api';

const mockGet = vi.mocked(getAwsOptions);
const mockRefresh = vi.mocked(refreshAwsOptions);
const mockProgress = vi.mocked(getAwsEnumProgress);
const mockSelect = store.selectAwsRoles;

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

const EKS = {
  accounts: [{ account_id: '111', account_name: 'acme' }],
  roles: [{ account_id: '111', role_name: 'eks-ro' }],
};

describe('AwsAccountRoleModal grant', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockProgress.mockResolvedValue({ phase: 'idle', done: 0, total: 0 });
    mockSelect.mockResolvedValue(undefined);
  });

  it('closes immediately after granting a non-eks role', async () => {
    mockGet.mockResolvedValue(CACHED); // ReadOnly only
    const onClose = vi.fn();
    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

    await waitFor(() => expect(screen.getByText('ReadOnly')).toBeInTheDocument());
    fireEvent.click(screen.getByText('ReadOnly'));
    fireEvent.click(screen.getByText('Donner accès'));

    await waitFor(() =>
      expect(mockSelect).toHaveBeenCalledWith('s1', [{ account_id: '111', role_name: 'ReadOnly' }])
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('stays open after granting an eks-* role, showing the discovery phase', async () => {
    mockGet.mockResolvedValue(EKS);
    // Discovery still running, so the modal must not auto-close.
    mockProgress.mockResolvedValue({ phase: 'discovering', done: 0, total: 1 });
    const onClose = vi.fn();
    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

    await waitFor(() => expect(screen.getByText('eks-ro')).toBeInTheDocument());
    fireEvent.click(screen.getByText('eks-ro'));
    fireEvent.click(screen.getByText('Donner accès'));

    await waitFor(() => expect(mockSelect).toHaveBeenCalled());
    // The eks gate keeps it open and surfaces the EKS discovery state.
    await waitFor(() =>
      expect(screen.getByText('Découverte des clusters EKS en cours…')).toBeInTheDocument()
    );
    expect(onClose).not.toHaveBeenCalled();
  });
});
