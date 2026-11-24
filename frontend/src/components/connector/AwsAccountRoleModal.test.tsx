import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';

vi.mock('@/services/api', () => ({
  // Mirror the real ApiError so the modal's `e instanceof api.ApiError` branch works.
  ApiError: class ApiError extends Error {
    constructor(public readonly status: number, message: string) {
      super(message);
      this.name = 'ApiError';
    }
  },
  getAwsOptions: vi.fn(),
  refreshAwsOptions: vi.fn(),
  getAwsEnumProgress: vi.fn().mockResolvedValue({ phase: 'idle', done: 0, total: 0 }),
}));

// The grant goes through the store action; stub it so we can assert the flow.
// getState() is used by the modal's 409 auto-recovery path (warm re-grant / device).
const store = vi.hoisted(() => ({
  selectAwsRoles: vi.fn(),
  tryWarmGrant: vi.fn(),
  startAwsDevice: vi.fn(),
}));
vi.mock('@/store/connectors', () => {
  const hook = (selector?: (s: typeof store) => unknown) =>
    selector ? selector(store) : store;
  hook.getState = () => store;
  return { useConnectorsStore: hook };
});

import { getAwsOptions, refreshAwsOptions, getAwsEnumProgress, ApiError } from '@/services/api';

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

  it('recovers from a 409 by silently warm-regranting and reloading the list', async () => {
    // First options call 409s (no pending auth); warm re-grant succeeds; reload works.
    mockGet
      .mockRejectedValueOnce(new ApiError(409, 'No pending AWS authentication; re-initiate'))
      .mockResolvedValueOnce(CACHED);
    store.tryWarmGrant.mockResolvedValue(true);

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={() => {}} />);

    await waitFor(() =>
      expect(store.tryWarmGrant).toHaveBeenCalledWith('s1', 'aws', { openPicker: false }),
    );
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('ReadOnly')).toBeInTheDocument());
    expect(store.startAwsDevice).not.toHaveBeenCalled();
  });

  it('falls back to the device flow and closes when there is no warm session', async () => {
    mockGet.mockRejectedValue(new ApiError(409, 'No pending AWS authentication; re-initiate'));
    store.tryWarmGrant.mockResolvedValue(false);
    const onClose = vi.fn();

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

    await waitFor(() => expect(store.startAwsDevice).toHaveBeenCalledWith('s1'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('does not loop: a second 409 after warm re-grant surfaces the error', async () => {
    // Warm "succeeds" but options keeps 409ing (e.g. already-connected cred) — the
    // one-shot guard must stop, not retry forever.
    mockGet.mockRejectedValue(new ApiError(409, 'No pending AWS authentication; re-initiate'));
    store.tryWarmGrant.mockResolvedValue(true);

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={() => {}} />);

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    // No third attempt, no device fallback (guard consumed) — settles, doesn't spin.
    await new Promise((r) => setTimeout(r, 30));
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(store.startAwsDevice).not.toHaveBeenCalled();
  });

  it('loads cached options on open, then re-enumerates on refresh click', async () => {
    mockGet.mockResolvedValue(CACHED);
    mockRefresh.mockResolvedValue(FRESH);

    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={() => {}} />);

    // Cached list shown first — no portal hit.
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('s1'));
    expect(await screen.findByText('ReadOnly')).toBeInTheDocument();
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();

    // Refresh button forces a fresh enumeration.
    fireEvent.click(screen.getByLabelText('Refresh the role list'));
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
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    // Refresh: Admin disappears → only ReadOnly stays selected.
    fireEvent.click(screen.getByLabelText('Refresh the role list'));
    await waitFor(() => expect(screen.queryByText('Admin')).not.toBeInTheDocument());
    expect(screen.getByText('1 selected')).toBeInTheDocument();
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
    fireEvent.click(screen.getByText('Grant access'));

    await waitFor(() =>
      expect(mockSelect).toHaveBeenCalledWith('s1', [{ account_id: '111', role_name: 'ReadOnly' }])
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('shows the per-pair minting count in the bar while the grant is in flight', async () => {
    mockGet.mockResolvedValue(CACHED);
    // Backend reports the "granting" phase during STS minting (e.g. 12 of 55 pairs).
    mockProgress.mockResolvedValue({ phase: 'granting', done: 12, total: 55 });
    // Keep the POST pending so `submitting` stays true and the bar keeps polling.
    let release: () => void = () => {};
    mockSelect.mockImplementation(
      () => new Promise<void>((r) => { release = () => r(); })
    );
    const onClose = vi.fn();
    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

    await waitFor(() => expect(screen.getByText('ReadOnly')).toBeInTheDocument());
    fireEvent.click(screen.getByText('ReadOnly'));
    fireEvent.click(screen.getByText('Grant access'));

    // Minting is visible as a live count, not just the spinner — the point for big grants.
    await waitFor(() =>
      expect(screen.getByText(/Creating access… 12\/55/)).toBeInTheDocument()
    );
    // Let the POST finish and flush the follow-up state updates so teardown is clean.
    await act(async () => {
      release();
    });
  });

  it('stays open after granting an eks-* role, showing the discovery phase', async () => {
    mockGet.mockResolvedValue(EKS);
    // Discovery still running, so the modal must not auto-close.
    mockProgress.mockResolvedValue({ phase: 'discovering', done: 0, total: 1 });
    const onClose = vi.fn();
    render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

    await waitFor(() => expect(screen.getByText('eks-ro')).toBeInTheDocument());
    fireEvent.click(screen.getByText('eks-ro'));
    fireEvent.click(screen.getByText('Grant access'));

    await waitFor(() => expect(mockSelect).toHaveBeenCalled());
    // The eks gate keeps it open and surfaces the EKS discovery state.
    await waitFor(() =>
      expect(screen.getByText('Discovering EKS clusters…')).toBeInTheDocument()
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it('auto-closes after an eks grant once discovery reports idle, honoring the 2s floor', async () => {
    vi.useFakeTimers();
    try {
      mockGet.mockResolvedValue(EKS);
      mockProgress
        .mockResolvedValueOnce({ phase: 'discovering', done: 0, total: 1 })
        .mockResolvedValue({ phase: 'idle', done: 0, total: 0 });
      const onClose = vi.fn();
      render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0); // flush initial load
      });
      fireEvent.click(screen.getByText('eks-ro'));
      fireEvent.click(screen.getByText('Grant access'));
      // Grant POST + first poll (discovering → arms sawProgress, records shownAt).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(onClose).not.toHaveBeenCalled();
      // Next poll → idle, but the 2s minimum-display floor keeps it open for now.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(onClose).not.toHaveBeenCalled();
      // Past the floor → auto-close fires exactly once.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('auto-closes on the terminal "done" even when the active discovery window was missed', async () => {
    vi.useFakeTimers();
    try {
      mockGet.mockResolvedValue(EKS);
      // The race the backend's terminal state fixes: discovery finished between polls, so
      // the picker never sees "discovering" — only the lingering "done". It must still close
      // (the old "saw active → then idle" path would have hung on the 90s safety net here).
      mockProgress.mockResolvedValue({ phase: 'done', done: 1, total: 1 });
      const onClose = vi.fn();
      render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0); // flush initial load
      });
      fireEvent.click(screen.getByText('eks-ro'));
      fireEvent.click(screen.getByText('Grant access'));
      // Grant POST + first poll → "done"; close fires within the MIN_PROGRESS_MS floor
      // (0 here, since no active poll ever armed the floor's start).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('idle-only discovery: sawProgress guard holds, then the 90s safety timeout closes', async () => {
    vi.useFakeTimers();
    try {
      mockGet.mockResolvedValue(EKS);
      // Never reports non-idle (e.g. cross-replica poll) → sawProgress never arms.
      mockProgress.mockResolvedValue({ phase: 'idle', done: 0, total: 0 });
      const onClose = vi.fn();
      render(<AwsAccountRoleModal sessionId="s1" isOpen onClose={onClose} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      fireEvent.click(screen.getByText('eks-ro'));
      fireEvent.click(screen.getByText('Grant access'));
      // Several idle polls must NOT close it (guard against premature close).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(onClose).not.toHaveBeenCalled();
      // The 90s safety net eventually force-closes so the picker never hangs.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(90_000);
      });
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
