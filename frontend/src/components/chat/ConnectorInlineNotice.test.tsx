import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConnectorInlineNotice } from './ConnectorInlineNotice';
import { ConnectorStatus } from '@/types/connectors';

const connectorStore = vi.hoisted(() => ({
  state: {
    statuses: {} as Record<string, ConnectorStatus>,
    reauthRequired: null as string | null,
  },
}));

vi.mock('@/store/connectors', () => ({
  useConnectorsStore: () => connectorStore.state,
}));

const grant = vi.hoisted(() => vi.fn());
const grantHook = vi.hoisted(() => ({
  busy: null as string | null,
  error: null as string | null,
}));
vi.mock('@/components/connector/useConnectorGrant', () => ({
  useConnectorGrant: () => ({ grant, busy: grantHook.busy, error: grantHook.error }),
}));

describe('ConnectorInlineNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connectorStore.state.statuses = {};
    connectorStore.state.reauthRequired = null;
    grantHook.busy = null;
    grantHook.error = null;
  });

  it('renders nothing when no connector needs attention', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'connected' } };
    const { container } = render(<ConnectorInlineNotice sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it('does not surface a connector that is not connected but also not required', () => {
    // A backend-enabled but un-granted connector should only appear in the header
    // panel, not as an in-conversation interruption, until a tool asks for it.
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'not_connected' } };
    const { container } = render(<ConnectorInlineNotice sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a grant action when a tool requires a not-connected connector', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'not_connected' } };
    connectorStore.state.reauthRequired = 'aws';

    render(<ConnectorInlineNotice sessionId="s1" />);

    expect(screen.getByText('This step needs AWS access')).toBeInTheDocument();
    expect(screen.getByText('not connected')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Give access'));
    expect(grant).toHaveBeenCalledWith('aws');
  });

  it('does not interrupt the flow once the required connector is connected again', () => {
    // Revoking an active grant lives in the header panel; a connected connector is
    // never an in-flow interruption here.
    connectorStore.state.statuses = {
      gitlab: { connector: 'gitlab', state: 'connected', expires_at: '2999-01-01T00:00:00Z' },
    };
    connectorStore.state.reauthRequired = 'gitlab';

    const { container } = render(<ConnectorInlineNotice sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it('surfaces an expired grant even without an explicit re-auth request', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'expired' } };

    render(<ConnectorInlineNotice sessionId="s1" />);

    expect(screen.getByText('AWS access expired')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Give access'));
    expect(grant).toHaveBeenCalledWith('aws');
  });

  it('offers the account picker when AWS is authenticated but needs a selection', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'needs_selection' } };
    connectorStore.state.reauthRequired = 'aws';

    render(<ConnectorInlineNotice sessionId="s1" />);

    fireEvent.click(screen.getByText('Choose account'));
    expect(grant).toHaveBeenCalledWith('aws');
  });

  it('surfaces a grant failure in the notice (does not swallow it)', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'not_connected' } };
    connectorStore.state.reauthRequired = 'aws';
    grantHook.error = 'Failed to start AWS authorization: boom';

    render(<ConnectorInlineNotice sessionId="s1" />);

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Failed to start AWS authorization: boom',
    );
  });

  it('stays pinned to the top of the scroll viewport (sticky) so it cannot scroll out of view', () => {
    connectorStore.state.statuses = { aws: { connector: 'aws', state: 'expired' } };

    render(<ConnectorInlineNotice sessionId="s1" />);

    expect(screen.getByTestId('connector-inline-notice')).toHaveClass('sticky');
  });
});
