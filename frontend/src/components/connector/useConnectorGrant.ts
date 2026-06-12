import { useState, useCallback } from 'react';
import { useConnectorsStore } from '@/store/connectors';
import { ConnectorName } from '@/types/connectors';
import { LABELS } from './connectorChrome';

// Shared "start a grant" behaviour for a single connector, reused by the header
// ConnectorPanel and the in-conversation notice. It only drives store actions —
// the AWS device / account-role modals are owned by the always-mounted
// ConnectorPanel, so triggering the grant from anywhere surfaces the same flow.
// `busy` is the store-shared grant-in-flight flag, so a grant started from one
// surface disables the same connector's button on the other.
export function useConnectorGrant(sessionId: string) {
  const { statuses, initiate, startAwsDevice, requestAwsModal, tryWarmGrant, grantBusy, setGrantBusy } =
    useConnectorsStore();
  const [error, setError] = useState<string | null>(null);

  const grant = useCallback(
    async (connector: ConnectorName) => {
      // Already authenticated but no account/role chosen yet: jump straight to the
      // picker instead of re-running device auth.
      if (connector === 'aws' && statuses[connector]?.state === 'needs_selection') {
        requestAwsModal();
        return;
      }
      // Shared in-flight guard: ignore a duplicate start for a connector already
      // being granted (e.g. the same connector clicked in both the header panel
      // and the in-conversation notice). Read live store state to avoid a stale
      // closure racing two near-simultaneous clicks.
      if (useConnectorsStore.getState().grantBusy === connector) return;
      setGrantBusy(connector);
      setError(null);
      try {
        // Warm platform auth (user logged in earlier today) → grant without a login
        // flow: AWS opens the account/role picker directly, GitLab connects silently.
        if (await tryWarmGrant(sessionId, connector)) return;
        if (connector === 'aws') {
          await startAwsDevice(sessionId); // device grant: code + URL, then polling
        } else {
          await initiate(sessionId, connector); // OAuth redirect (GitLab)
        }
      } catch (e) {
        const detail = e instanceof Error && e.message ? `: ${e.message}` : '';
        setError(`Failed to start ${LABELS[connector]} authorization${detail}`);
      } finally {
        setGrantBusy(null);
      }
    },
    [sessionId, statuses, initiate, startAwsDevice, requestAwsModal, tryWarmGrant, setGrantBusy]
  );

  return { grant, busy: grantBusy, error };
}
