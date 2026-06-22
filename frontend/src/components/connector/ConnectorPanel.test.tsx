import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConnectorPanel } from './ConnectorPanel';

const connectorStore = vi.hoisted(() => ({
  state: {
    statuses: {},
    isLoading: false,
    error: null,
    reauthRequired: null,
    awsDevice: null as null | { userCode: string; verificationUri: string; status: string },
    awsModalRequested: false,
    renewable: [],
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

describe('ConnectorPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connectorStore.state.statuses = {};
    connectorStore.state.renewable = [];
    connectorStore.state.awsDevice = null;
    connectorStore.state.awsModalRequested = false;
    connectorStore.state.reauthRequired = null;
    enumProgress.value = null;
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
});
