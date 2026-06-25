import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ConnectorPanel } from './ConnectorPanel';

const connectorStore = vi.hoisted(() => ({
  state: {
    statuses: {} as Record<string, { state: string; expires_at?: string | null }>,
    isLoading: false,
    error: null,
    reauthRequired: null as null | string,
    awsDevice: null as null | { userCode: string; verificationUri: string; status: string },
    awsModalRequested: false,
    renewable: [] as Array<{ connector: string }>,
    awsAutoSelect: false,
    grantBusy: null,
    setGrantBusy: vi.fn(),
    fetchStatus: vi.fn(),
    fetchRenewable: vi.fn(),
    renewAll: vi.fn(),
    initiate: vi.fn(),
    startAwsDevice: vi.fn(),
    pollAwsDevice: vi.fn(),
    openAwsVerification: vi.fn(),
    closeAwsVerification: vi.fn(),
    clearAwsDevice: vi.fn(),
    requestAwsModal: vi.fn(),
    clearAwsModalRequest: vi.fn(),
    selectAwsRoles: vi.fn(),
    revoke: vi.fn(),
    setReauthRequired: vi.fn(),
    reset: vi.fn(),
  },
}));

vi.mock('@/store/connectors', () => ({
  useConnectorsStore: (selector?: (state: typeof connectorStore.state) => unknown) =>
    selector ? selector(connectorStore.state) : connectorStore.state,
}));

// Drive enumeration progress directly so the popup-close effect can be exercised
// without faking the 1s poll timer / api.
const enumProgress = vi.hoisted(() => ({ value: null as null | { done: number; total: number } }));
vi.mock('@/hooks/useAwsEnumProgress', () => ({
  useAwsEnumProgress: () => enumProgress.value,
}));

vi.mock('./McpPanel', () => ({
  McpPanel: ({ showWhenEmpty }: { showWhenEmpty?: boolean }) => (
    <div data-testid="mcp-panel" data-show-when-empty={String(showWhenEmpty)}>
      MCP panel
    </div>
  ),
}));

// Drive the shared grant hook directly so the icons layout can be exercised without
// running the real device/OAuth flow: we control busy/error/errorConnector per test.
const grant = vi.hoisted(() => ({
  fn: vi.fn(),
  busy: null as null | string,
  error: null as null | string,
  errorConnector: null as null | string,
}));
vi.mock('./useConnectorGrant', () => ({
  useConnectorGrant: () => ({
    grant: grant.fn,
    busy: grant.busy,
    error: grant.error,
    errorConnector: grant.errorConnector,
  }),
}));

describe('ConnectorPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connectorStore.state.statuses = {};
    connectorStore.state.renewable = [];
    connectorStore.state.awsDevice = null;
    connectorStore.state.awsModalRequested = false;
    connectorStore.state.reauthRequired = null;
    enumProgress.value = null;
    grant.busy = null;
    grant.error = null;
    grant.errorConnector = null;
  });

  it('keeps the MCP row mounted in stacked mode while connector statuses are empty', () => {
    render(<ConnectorPanel sessionId="s1" layout="stacked" />);

    expect(screen.getByTestId('mcp-panel')).toHaveAttribute('data-show-when-empty', 'true');
    expect(connectorStore.state.fetchStatus).toHaveBeenCalledWith('s1');
    expect(connectorStore.state.fetchRenewable).toHaveBeenCalledWith('s1');
  });

  it('still renders nothing in inline mode when no connectors are enabled', () => {
    const { container } = render(<ConnectorPanel sessionId="s1" />);

    expect(container.firstChild).toBeNull();
  });

  it('closes the AWS popup once when enumeration starts (not on every tick)', () => {
    // Device flow pending + enumeration reporting progress = code just validated.
    connectorStore.state.awsDevice = {
      userCode: 'ABCD-EFGH',
      verificationUri: 'https://aws/verify',
      status: 'pending',
    };
    enumProgress.value = { done: 1, total: 3 };
    const { rerender } = render(<ConnectorPanel sessionId="s1" />);
    expect(connectorStore.state.closeAwsVerification).toHaveBeenCalledTimes(1);

    // A later poll tick (new object, higher count) must NOT close it again.
    enumProgress.value = { done: 2, total: 3 };
    rerender(<ConnectorPanel sessionId="s1" />);
    expect(connectorStore.state.closeAwsVerification).toHaveBeenCalledTimes(1);
  });

  // --- icons layout (composer toolbar) ---

  it('icons layout surfaces a grant error on the connector that failed (H1)', () => {
    connectorStore.state.statuses = { aws: { state: 'not_connected' } };
    grant.error = 'Failed to start AWS authorization';
    grant.errorConnector = 'aws';
    render(<ConnectorPanel sessionId="s1" layout="icons" />);

    // The failure is visible (red text) and folded into the icon's accessible name,
    // not swallowed when the spinner stops (the bug H1 was about).
    expect(screen.getByText('Failed to start AWS authorization')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /AWS — Failed to start AWS authorization/ }),
    ).toBeInTheDocument();
  });

  it('icons layout shows a renew icon when a prior grant is renewable, click renews all (H1)', async () => {
    connectorStore.state.statuses = { aws: { state: 'expired' } };
    connectorStore.state.renewable = [{ connector: 'aws' }];
    render(<ConnectorPanel sessionId="s1" layout="icons" />);

    fireEvent.click(screen.getByRole('button', { name: /Renew access/ }));
    // handleRenew is async (sets a renewing flag around the call); await the effect.
    await waitFor(() => expect(connectorStore.state.renewAll).toHaveBeenCalledWith('s1'));
  });

  it('icons layout: no renew icon when nothing is renewable', () => {
    connectorStore.state.statuses = { aws: { state: 'not_connected' } };
    render(<ConnectorPanel sessionId="s1" layout="icons" />);

    expect(screen.queryByRole('button', { name: /Renew access/ })).not.toBeInTheDocument();
  });

  it('icons layout toggles: disconnected connector grants, connected one revokes', () => {
    connectorStore.state.statuses = {
      aws: { state: 'not_connected' },
      gitlab: { state: 'connected' },
    };
    render(<ConnectorPanel sessionId="s1" layout="icons" />);

    fireEvent.click(screen.getByRole('button', { name: /Give AWS access/ }));
    expect(grant.fn).toHaveBeenCalledWith('aws');

    fireEvent.click(screen.getByRole('button', { name: /GitLab — connected, click to revoke/ }));
    expect(connectorStore.state.revoke).toHaveBeenCalledWith('s1', 'gitlab');
  });
});
