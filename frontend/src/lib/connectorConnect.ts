import * as api from '@/services/api';
import { ConnectorName } from '@/types/connectors';

/**
 * Open the OAuth consent popup for an OAuth connector (GitLab), popup-blocker safe.
 *
 * Mirror of {@link openMcpAuthPopup}: the window MUST be opened synchronously inside the
 * click handler (before any await), then navigated to the IdP authorize URL once the
 * backend returns it — opening it after the await trips popup blockers. The backend
 * callback returns a self-closing page, so the main window never navigates; the caller
 * polls connector status until the popup closes. Returns the opened window for that poll.
 */
export function openConnectorAuthPopup(
  sessionId: string,
  connector: ConnectorName,
  onError?: () => void
): Window | null {
  const w = 600;
  const h = 720;
  const left = Math.max(0, Math.round((window.screen.width - w) / 2));
  const top = Math.max(0, Math.round((window.screen.height - h) / 2));
  const popup = window.open(
    'about:blank',
    'connector-oauth',
    `width=${w},height=${h},left=${left},top=${top}`
  );
  void (async () => {
    try {
      const { redirect_url } = await api.initiateConnector(sessionId, connector);
      if (popup) popup.location.href = redirect_url;
    } catch {
      popup?.close();
      onError?.();
    }
  })();
  return popup;
}
