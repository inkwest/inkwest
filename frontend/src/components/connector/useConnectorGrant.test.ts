import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useConnectorGrant } from './useConnectorGrant';
import { useConnectorsStore } from '@/store/connectors';
import * as api from '@/services/api';

vi.mock('@/services/api', () => ({
  startAwsDevice: vi.fn(),
}));

const startAwsDevice = vi.mocked(api.startAwsDevice);

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useConnectorGrant', () => {
  beforeEach(() => {
    useConnectorsStore.getState().reset();
    startAwsDevice.mockReset();
  });

  it('ignores a duplicate grant start for a connector already in flight (shared across surfaces)', async () => {
    const d = deferred<api.AwsDeviceStart>();
    startAwsDevice.mockReturnValue(d.promise);

    const { result } = renderHook(() => useConnectorGrant('s1'));

    let first!: Promise<void>;
    act(() => {
      first = result.current.grant('aws');
    });
    // First start is in flight and the shared busy flag is set.
    expect(useConnectorsStore.getState().grantBusy).toBe('aws');

    // A second start for the same connector (e.g. clicked in the other surface) is dropped.
    await act(async () => {
      await result.current.grant('aws');
    });
    expect(startAwsDevice).toHaveBeenCalledTimes(1);

    await act(async () => {
      d.resolve({
        user_code: 'X',
        verification_uri: 'https://example/verify',
        verification_uri_complete: 'https://example/verify?code=X',
        interval: 5,
        expires_in: 600,
      });
      await first;
    });
    expect(useConnectorsStore.getState().grantBusy).toBeNull();
  });

  it('routes AWS needs_selection to the picker without starting device auth', async () => {
    useConnectorsStore.setState({
      statuses: { aws: { connector: 'aws', state: 'needs_selection' } },
    });

    const { result } = renderHook(() => useConnectorGrant('s1'));
    await act(async () => {
      await result.current.grant('aws');
    });

    expect(useConnectorsStore.getState().awsModalRequested).toBe(true);
    expect(startAwsDevice).not.toHaveBeenCalled();
    expect(useConnectorsStore.getState().grantBusy).toBeNull();
  });

  it('captures a start failure as a user-facing error and clears busy', async () => {
    startAwsDevice.mockRejectedValue(new Error('boom'));

    const { result } = renderHook(() => useConnectorGrant('s1'));
    await act(async () => {
      await result.current.grant('aws');
    });

    expect(result.current.error).toContain('Failed to start AWS authorization');
    expect(result.current.error).toContain('boom');
    expect(useConnectorsStore.getState().grantBusy).toBeNull();
  });
});
