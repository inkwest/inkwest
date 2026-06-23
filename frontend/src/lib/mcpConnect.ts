import { startMcpAuth } from '@/services/api';

/**
 * Open the OAuth consent popup for an MCP server, popup-blocker safe.
 *
 * The window MUST be opened synchronously inside the click handler (before any await), then
 * navigated to the authorization URL once the backend returns it — opening it after the await
 * trips popup blockers. Returns the opened window so the caller can poll until it closes.
 */
export function openMcpAuthPopup(key: string, onError?: () => void): Window | null {
  // Mid-size, gently landscape (between square and 16:9), centred on the screen.
  const w = 820;
  const h = 600;
  const left = Math.max(0, Math.round((window.screen.width - w) / 2));
  const top = Math.max(0, Math.round((window.screen.height - h) / 2));
  const popup = window.open(
    'about:blank',
    'mcp-oauth',
    `width=${w},height=${h},left=${left},top=${top}`,
  );
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
