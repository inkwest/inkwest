import * as api from '@/services/api';
import { ConnectorName } from '@/types/connectors';
import { openAuthPopup } from '@/lib/oauthPopup';

/**
 * Open the OAuth consent popup for an OAuth connector (GitLab), popup-blocker safe
 * (see {@link openAuthPopup}). The backend callback returns a self-closing page, so the main
 * window never navigates; the caller polls connector status until the popup closes. Returns the
 * opened window for that poll.
 */
export function openConnectorAuthPopup(
  sessionId: string,
  connector: ConnectorName,
  onError?: () => void
): Window | null {
  return openAuthPopup(
    'connector-oauth',
    { width: 600, height: 720 },
    async () => (await api.initiateConnector(sessionId, connector)).redirect_url,
    () => onError?.(),
  );
}
