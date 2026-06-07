import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useConnectorsStore } from './connectors';
import * as api from '@/services/api';
import { ConnectorStatus } from '@/types/connectors';

describe('connectorsStore', () => {
  beforeEach(() => {
    useConnectorsStore.getState().reset();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('starts empty', () => {
    const s = useConnectorsStore.getState();
    expect(s.statuses).toEqual({});
    expect(s.reauthRequired).toBeNull();
  });

  it('indexes fetched statuses by connector', async () => {
    const list: ConnectorStatus[] = [
      { connector: 'aws', state: 'not_connected' },
      { connector: 'gitlab', state: 'connected', expires_at: '2030-01-01T00:00:00Z' },
    ];
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue(list);

    await useConnectorsStore.getState().fetchStatus('sess-1');

    const { statuses } = useConnectorsStore.getState();
    expect(statuses.aws.state).toBe('not_connected');
    expect(statuses.gitlab.state).toBe('connected');
  });

  it('updates a single connector status on coralogix activation', async () => {
    vi.spyOn(api, 'activateCoralogix').mockResolvedValue({
      connector: 'coralogix',
      state: 'connected',
    });
    await useConnectorsStore.getState().activateCoralogix('sess-1');
    expect(useConnectorsStore.getState().statuses.coralogix.state).toBe('connected');
  });

  it('clears reauthRequired when the same connector is revoked', async () => {
    useConnectorsStore.getState().setReauthRequired('aws');
    vi.spyOn(api, 'revokeConnector').mockResolvedValue({
      connector: 'aws',
      state: 'not_connected',
    });
    await useConnectorsStore.getState().revoke('sess-1', 'aws');
    const s = useConnectorsStore.getState();
    expect(s.statuses.aws.state).toBe('not_connected');
    expect(s.reauthRequired).toBeNull();
  });
});
