import { useEffect, useRef } from 'react';
import { useMcpStore } from '@/store/mcp';
import { useMcpOauthConnect } from '@/hooks/useMcpOauthConnect';

/**
 * In-conversation banner for OAuth2 MCP servers the backend flagged as needing a reconnect
 * (an `mcp_auth_required` event — an enabled server whose grant is missing/expired/revoked).
 * Mirrors ConnectorInlineNotice: a first-class "Reconnect" button instead of the tools
 * silently vanishing. Clears a key once it reconnects (catalog status flips to `connected`).
 */
export function McpInlineNotice({ sessionId }: { sessionId?: string }) {
  const authRequired = useMcpStore((s) => s.authRequired);
  const clearAuthRequired = useMcpStore((s) => s.clearAuthRequired);
  const prevSession = useRef<string | undefined>(undefined);
  // Shared connect+poll: clears the banner for a key once its grant lands (the poll force-fetches
  // the catalog, so it also clears when the user reconnects via the panel while polling).
  const { connect: reconnect } = useMcpOauthConnect((_servers, key) => clearAuthRequired(key));

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
