import { useEffect, useRef } from 'react';
import { getMcpCatalog } from '@/services/api';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { useMcpStore } from '@/store/mcp';

/**
 * In-conversation banner for OAuth2 MCP servers the backend flagged as needing a reconnect
 * (an `mcp_auth_required` event — an enabled server whose grant is missing/expired/revoked).
 * Mirrors ConnectorInlineNotice: a first-class "Reconnect" button instead of the tools
 * silently vanishing. Clears a key once it reconnects (catalog status flips to `connected`).
 */
export function McpInlineNotice({ sessionId }: { sessionId?: string }) {
  const authRequired = useMcpStore((s) => s.authRequired);
  const clearAuthRequired = useMcpStore((s) => s.clearAuthRequired);
  // Track poll timers so they're always cleared on unmount (never poll forever).
  const timers = useRef<Set<number>>(new Set());
  // Keys with an in-flight reconnect poll, so a double-click doesn't spawn a second popup+poll.
  const reconnecting = useRef<Set<string>>(new Set());
  const prevSession = useRef<string | undefined>(undefined);

  useEffect(() => {
    const live = timers.current;
    return () => live.forEach((t) => window.clearInterval(t));
  }, []);

  // The flagged set is global but enablement is per-conversation — clear it when the session
  // actually CHANGES (not on first mount) so a banner from session A doesn't linger on B (B
  // re-flags on its own next turn).
  useEffect(() => {
    if (prevSession.current !== undefined && prevSession.current !== sessionId) {
      useMcpStore.setState({ authRequired: [] });
    }
    prevSession.current = sessionId;
  }, [sessionId]);

  if (authRequired.length === 0) return null;

  const reconnect = (key: string) => {
    if (reconnecting.current.has(key)) return; // a poll is already in flight for this key
    openMcpAuthPopup(key);
    reconnecting.current.add(key);
    const startedAt = Date.now();
    const DEADLINE_MS = 5 * 60 * 1000;
    // Poll the catalog (not the popup) until the grant lands or a deadline — so the banner
    // also clears when the user reconnects via the panel or the popup was blocked.
    const timer = window.setInterval(async () => {
      let connected = false;
      try {
        const cat = await getMcpCatalog();
        connected = cat.servers.find((s) => s.key === key)?.auth_status === 'connected';
      } catch {
        /* transient; keep polling until connected or the deadline */
      }
      if (connected) clearAuthRequired(key);
      if (connected || Date.now() - startedAt > DEADLINE_MS) {
        window.clearInterval(timer);
        timers.current.delete(timer);
        reconnecting.current.delete(key);
      }
    }, 2000);
    timers.current.add(timer);
  };

  return (
    <div className="sticky top-0 z-10 mb-3 space-y-2">
      {authRequired.map((key) => (
        <div
          key={key}
          className="flex items-center justify-between gap-3 rounded-md border border-yellow-700/50 bg-yellow-900/30 px-3 py-2 text-sm text-yellow-200"
        >
          <span>
            MCP server <strong>{key}</strong> needs reconnecting.
          </span>
          <button
            onClick={() => reconnect(key)}
            className="shrink-0 rounded bg-yellow-700/60 px-2 py-1 text-xs text-yellow-50 hover:bg-yellow-700"
          >
            Reconnect
          </button>
        </div>
      ))}
    </div>
  );
}
