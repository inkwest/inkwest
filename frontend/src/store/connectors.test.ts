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

  it('clears reauthRequired once a fetched status shows the connector connected', async () => {
    useConnectorsStore.getState().setReauthRequired('gitlab');
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'gitlab', state: 'connected', expires_at: '2030-01-01T00:00:00Z' },
    ]);
    await useConnectorsStore.getState().fetchStatus('sess-1');
    expect(useConnectorsStore.getState().reauthRequired).toBeNull();
  });

  it('keeps reauthRequired when the fetched status is still not connected', async () => {
    useConnectorsStore.getState().setReauthRequired('aws');
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'aws', state: 'expired' },
    ]);
    await useConnectorsStore.getState().fetchStatus('sess-1');
    expect(useConnectorsStore.getState().reauthRequired).toBe('aws');
  });

  it('clears reauthRequired for AWS after a successful account/role selection', async () => {
    useConnectorsStore.getState().setReauthRequired('aws');
    vi.spyOn(api, 'selectAwsRoles').mockResolvedValue({ connector: 'aws', state: 'connected' });
    await useConnectorsStore
      .getState()
      .selectAwsRoles('sess-1', [{ account_id: '1', role_name: 'r' }]);
    expect(useConnectorsStore.getState().reauthRequired).toBeNull();
  });

  it('requestAwsModal asks the panel to open the picker', () => {
    useConnectorsStore.getState().requestAwsModal();
    expect(useConnectorsStore.getState().awsModalRequested).toBe(true);
  });
});

describe('connectorsStore renewal', () => {
  beforeEach(() => {
    useConnectorsStore.getState().reset();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetchRenewable populates the renewable set', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'aws', live: false, state: 'not_connected', aws_selection: { profiles: [] } },
    ]);
    await useConnectorsStore.getState().fetchRenewable('sess-1');
    expect(useConnectorsStore.getState().renewable.map((g) => g.connector)).toEqual(['aws']);
  });

  it('renewAll skips connectors that are already connected', async () => {
    // AWS connected, GitLab not — renewing must NOT re-run the AWS device flow.
    useConnectorsStore.setState({ statuses: { aws: { connector: 'aws', state: 'connected' } } });
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'aws', live: true, state: 'connected' },
      { connector: 'gitlab', live: false, state: 'not_connected' },
    ]);
    const startAws = vi.spyOn(api, 'startAwsDevice');
    vi.spyOn(api, 'gitlabRenew').mockResolvedValue({ connector: 'gitlab', state: 'connected' });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([]);

    await useConnectorsStore.getState().renewAll('sess-1');
    expect(startAws).not.toHaveBeenCalled();
    expect(useConnectorsStore.getState().awsAutoSelect).toBe(false);
  });

  it('renewAll refreshes GitLab silently when possible', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'gitlab', live: true, state: 'expired' },
    ]);
    const renew = vi
      .spyOn(api, 'gitlabRenew')
      .mockResolvedValue({ connector: 'gitlab', state: 'connected' });
    // fetchStatus (the final reconcile) is the source of truth for the badge.
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'gitlab', state: 'connected' },
    ]);

    await useConnectorsStore.getState().renewAll('sess-1');
    expect(renew).toHaveBeenCalledWith('sess-1');
    expect(useConnectorsStore.getState().statuses.gitlab.state).toBe('connected');
  });

  it('renewAll redirects to GitLab OAuth when it cannot refresh silently (no AWS pending)', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'gitlab', live: false, state: 'not_connected' },
    ]);
    vi.spyOn(api, 'gitlabRenew').mockRejectedValue(new Error('409'));
    const initiate = vi
      .spyOn(api, 'initiateConnector')
      .mockResolvedValue({ redirect_url: 'https://gitlab.example/oauth/authorize' });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([]);

    await useConnectorsStore.getState().renewAll('sess-1');
    expect(initiate).toHaveBeenCalledWith('sess-1', 'gitlab');
  });

  it('renewAll defers the GitLab OAuth redirect until AWS finishes', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'gitlab', live: false, state: 'not_connected' },
      { connector: 'aws', live: false, state: 'not_connected' },
    ]);
    vi.spyOn(api, 'gitlabRenew').mockRejectedValue(new Error('409'));
    vi.spyOn(api, 'grantFromWarm').mockRejectedValue(new Error('409')); // no warm grant
    const initiate = vi.spyOn(api, 'initiateConnector');
    vi.spyOn(api, 'startAwsDevice').mockResolvedValue({
      user_code: 'X',
      verification_uri: 'u',
      verification_uri_complete: 'uc',
      interval: 5,
      expires_in: 600,
    });

    await useConnectorsStore.getState().renewAll('sess-1');
    const s = useConnectorsStore.getState();
    expect(s.awsAutoSelect).toBe(true);
    expect(s.gitlabReauthPending).toBe(true); // queued, not fired yet
    expect(initiate).not.toHaveBeenCalled();
  });

  it('pollAwsDevice fires the deferred GitLab redirect once AWS renews', async () => {
    useConnectorsStore.setState({
      awsAutoSelect: true,
      gitlabReauthPending: true,
      awsDevice: { userCode: 'X', verificationUri: 'u', status: 'pending' },
    });
    vi.spyOn(api, 'pollAwsDevice').mockResolvedValue({ status: 'needs_selection' });
    vi.spyOn(api, 'awsRenewSelect').mockResolvedValue({ connector: 'aws', state: 'connected' });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'aws', state: 'connected' },
    ]);
    const initiate = vi
      .spyOn(api, 'initiateConnector')
      .mockResolvedValue({ redirect_url: 'https://gitlab.example/oauth/authorize' });

    await useConnectorsStore.getState().pollAwsDevice('sess-1');
    expect(initiate).toHaveBeenCalledWith('sess-1', 'gitlab');
    expect(useConnectorsStore.getState().gitlabReauthPending).toBe(false);
  });

  it('renewAll starts AWS device auth and arms auto-select', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'aws', live: false, state: 'not_connected' },
    ]);
    vi.spyOn(api, 'grantFromWarm').mockRejectedValue(new Error('409')); // no warm grant
    vi.spyOn(api, 'startAwsDevice').mockResolvedValue({
      user_code: 'ABCD-1234',
      verification_uri: 'https://device.aws',
      verification_uri_complete: 'https://device.aws?code=ABCD-1234',
      interval: 5,
      expires_in: 600,
    });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([]);

    await useConnectorsStore.getState().renewAll('sess-1');
    expect(useConnectorsStore.getState().awsAutoSelect).toBe(true);
    expect(useConnectorsStore.getState().awsDevice?.status).toBe('pending');
  });

  it('pollAwsDevice auto-applies the saved selection on needs_selection', async () => {
    useConnectorsStore.setState({
      awsAutoSelect: true,
      awsDevice: { userCode: 'X', verificationUri: 'u', status: 'pending' },
    });
    vi.spyOn(api, 'pollAwsDevice').mockResolvedValue({ status: 'needs_selection' });
    const renewSelect = vi
      .spyOn(api, 'awsRenewSelect')
      .mockResolvedValue({ connector: 'aws', state: 'connected' });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'aws', state: 'connected' },
    ]);

    const status = await useConnectorsStore.getState().pollAwsDevice('sess-1');
    expect(status).toBe('needs_selection');
    expect(renewSelect).toHaveBeenCalledWith('sess-1');
    const s = useConnectorsStore.getState();
    expect(s.awsModalRequested).toBe(false); // did NOT open the manual picker
    expect(s.awsAutoSelect).toBe(false);
    expect(s.statuses.aws.state).toBe('connected');
  });

  it('pollAwsDevice falls back to the manual picker when the saved selection is invalid', async () => {
    useConnectorsStore.setState({
      awsAutoSelect: true,
      awsDevice: { userCode: 'X', verificationUri: 'u', status: 'pending' },
    });
    vi.spyOn(api, 'pollAwsDevice').mockResolvedValue({ status: 'needs_selection' });
    vi.spyOn(api, 'awsRenewSelect').mockRejectedValue(new Error('400'));

    await useConnectorsStore.getState().pollAwsDevice('sess-1');
    const s = useConnectorsStore.getState();
    expect(s.awsModalRequested).toBe(true); // fell back to manual picker
    expect(s.awsAutoSelect).toBe(false);
  });

  it('tryWarmGrant grants GitLab and reports success', async () => {
    vi.spyOn(api, 'grantFromWarm').mockResolvedValue({ connector: 'gitlab', state: 'connected' });
    const ok = await useConnectorsStore.getState().tryWarmGrant('sess-1', 'gitlab');
    expect(ok).toBe(true);
    expect(useConnectorsStore.getState().statuses.gitlab.state).toBe('connected');
  });

  it('tryWarmGrant opens the AWS picker on needs_selection by default', async () => {
    vi.spyOn(api, 'grantFromWarm').mockResolvedValue({
      connector: 'aws',
      state: 'needs_selection',
    });
    const ok = await useConnectorsStore.getState().tryWarmGrant('sess-1', 'aws');
    expect(ok).toBe(true);
    expect(useConnectorsStore.getState().awsModalRequested).toBe(true);
  });

  it('tryWarmGrant with openPicker=false does not flash the AWS picker', async () => {
    vi.spyOn(api, 'grantFromWarm').mockResolvedValue({
      connector: 'aws',
      state: 'needs_selection',
    });
    const ok = await useConnectorsStore
      .getState()
      .tryWarmGrant('sess-1', 'aws', { openPicker: false });
    expect(ok).toBe(true);
    expect(useConnectorsStore.getState().awsModalRequested).toBe(false);
  });

  it('tryWarmGrant returns false on a warm miss (409)', async () => {
    vi.spyOn(api, 'grantFromWarm').mockRejectedValue(new Error('409'));
    const ok = await useConnectorsStore.getState().tryWarmGrant('sess-1', 'gitlab');
    expect(ok).toBe(false);
  });

  it('renewAll grants AWS from warm without a device flow', async () => {
    vi.spyOn(api, 'getRenewable').mockResolvedValue([
      { connector: 'aws', live: false, state: 'not_connected', aws_selection: { profiles: [] } },
    ]);
    vi.spyOn(api, 'grantFromWarm').mockResolvedValue({
      connector: 'aws',
      state: 'needs_selection',
    });
    const startAws = vi.spyOn(api, 'startAwsDevice');
    const renewSelect = vi
      .spyOn(api, 'awsRenewSelect')
      .mockResolvedValue({ connector: 'aws', state: 'connected' });
    vi.spyOn(api, 'getConnectorStatus').mockResolvedValue([
      { connector: 'aws', state: 'connected' },
    ]);

    await useConnectorsStore.getState().renewAll('sess-1');
    expect(startAws).not.toHaveBeenCalled(); // warm path skips the device flow
    expect(renewSelect).toHaveBeenCalledWith('sess-1');
    expect(useConnectorsStore.getState().awsModalRequested).toBe(false); // no picker flash
  });

  it('reset clears renewal state', () => {
    useConnectorsStore.setState({
      renewable: [{ connector: 'aws', live: false, state: 'not_connected' }],
      awsAutoSelect: true,
    });
    useConnectorsStore.getState().reset();
    expect(useConnectorsStore.getState().renewable).toEqual([]);
    expect(useConnectorsStore.getState().awsAutoSelect).toBe(false);
  });
});
