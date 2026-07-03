import { useCallback, useEffect, useRef, useState } from 'react';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { loadMcpCatalog } from '@/components/connector/mcpCatalogCache';
import type { McpServerEntry } from '@/services/api';

const POLL_INTERVAL_MS = 2000;
const POLL_DEADLINE_MS = 5 * 60 * 1000;

/**
 * Drive the OAuth2 MCP consent popup + connection poll. Opens the popup (blocker-safe, must be
 * called straight from a click handler) and polls the catalog until the server's grant lands
 * ("connected"), the popup closes, or a hard deadline elapses — so an abandoned popup can't poll
 * forever. `onConnected(servers, key)` fires once the grant lands, with the refreshed catalog
 * servers and the key that connected (each tick force-fetches through loadMcpCatalog, which also
 * primes the shared cache so other surfaces observe the reconnect). All timers are cleared on
 * unmount.
 *
 * The single connect+poll implementation for every surface (McpPanel, McpInlineNotice, Settings).
 */
export function useMcpOauthConnect(
  onConnected?: (servers: McpServerEntry[], key: string) => void,
) {
  const [connectingKeys, setConnectingKeys] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  // In-flight guard (synchronous) so a double-click can't spawn a second popup + poll.
  const connecting = useRef<Set<string>>(new Set());
  const pollTimers = useRef<Set<number>>(new Set());
  // Latest callback without re-creating `connect` (and thus without restarting live polls).
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;

  useEffect(() => {
    const timers = pollTimers.current;
    return () => timers.forEach((t) => window.clearInterval(t));
  }, []);

  const connect = useCallback((key: string) => {
    // Clear any stale error BEFORE the in-flight guard so a re-click always gives feedback.
    setError(null);
    if (connecting.current.has(key)) return;
    const popup = openMcpAuthPopup(key, (m) => setError(`Failed to start MCP authorization: ${m}`));
    connecting.current.add(key);
    setConnectingKeys((prev) => new Set(prev).add(key));

    const startedAt = Date.now();
    const timer = window.setInterval(async () => {
      let servers: McpServerEntry[] = [];
      try {
        servers = (await loadMcpCatalog(true)).servers;
      } catch {
        /* transient; keep polling until connected, popup close, or the deadline */
      }
      const connected = servers.find((s) => s.key === key)?.auth_status === 'connected';
      // Stop on: grant landed, the user CLOSED the popup (abandon), or the deadline. A blocked
      // popup (window.open → null) is NOT an abandon — keep polling so the state still clears if
      // the user connects another way (e.g. via the panel); `popup?.closed` stays falsy for null.
      if (connected || popup?.closed || Date.now() - startedAt > POLL_DEADLINE_MS) {
        window.clearInterval(timer);
        pollTimers.current.delete(timer);
        connecting.current.delete(key);
        setConnectingKeys((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        if (connected) {
          setError(null); // a late success clears an error raised earlier this attempt
          // Close the popup from the opener (its own window.close() is blocked post round-trip).
          try {
            popup?.close();
          } catch {
            /* already closed / cross-origin — nothing to do */
          }
          onConnectedRef.current?.(servers, key);
        }
      }
    }, POLL_INTERVAL_MS);
    pollTimers.current.add(timer);
  }, []);

  return { connect, connectingKeys, error, setError };
}
