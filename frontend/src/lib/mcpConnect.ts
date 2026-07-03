import { startMcpAuth } from '@/services/api';
import { openAuthPopup } from '@/lib/oauthPopup';

/**
 * Open the OAuth consent popup for an MCP server, popup-blocker safe (see {@link openAuthPopup}).
 * Returns the opened window so the caller can poll until it closes.
 */
export function openMcpAuthPopup(
  key: string,
  onError?: (message: string) => void,
): Window | null {
  // Mid-size, gently landscape (between square and 16:9).
  return openAuthPopup(
    'mcp-oauth',
    { width: 960, height: 740 },
    async () => (await startMcpAuth(key)).authorize_url,
    // Surface the backend's reason (ApiError carries the 502 detail, e.g. a discovery failure)
    // instead of a blank popup — the generic fallback covers non-Error throws.
    (e) => onError?.(e instanceof Error ? e.message : 'Failed to start MCP authorization'),
  );
}
