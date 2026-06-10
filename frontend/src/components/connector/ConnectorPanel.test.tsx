import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConnectorPanel } from './ConnectorPanel';

const connectorStore = vi.hoisted(() => ({
  state: {
    statuses: {},
    isLoading: false,
    error: null,
    reauthRequired: null,
    awsDevice: null,
    awsModalRequested: false,
    renewable: [],
    awsAutoSelect: false,
    fetchStatus: vi.fn(),
    fetchRenewable: vi.fn(),
    renewAll: vi.fn(),
    initiate: vi.fn(),
    startAwsDevice: vi.fn(),
    pollAwsDevice: vi.fn(),
    openAwsVerification: vi.fn(),
    clearAwsDevice: vi.fn(),
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
});
