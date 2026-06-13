import { startMcpAuth } from '@/services/api';

/**
 * Open the OAuth consent popup for an MCP server, popup-blocker safe.
 *
 * The window MUST be opened synchronously inside the click handler (before any await), then
 * navigated to the authorization URL once the backend returns it — opening it after the await
 * trips popup blockers. Returns the opened window so the caller can poll until it closes.
 */
export function openMcpAuthPopup(key: string, onError?: () => void): Window | null {
  const popup = window.open('about:blank', 'mcp-oauth', 'width=520,height=720');
  void (async () => {
    try {
      const { authorize_url } = await startMcpAuth(key);
      if (popup) popup.location.href = authorize_url;
    } catch {
      popup?.close();
      onError?.();
    }
  })();
  return popup;
}
