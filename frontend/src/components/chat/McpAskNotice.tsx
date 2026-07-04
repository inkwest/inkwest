import { useEffect, useRef } from 'react';
import { getPendingMcpAsks } from '@/services/api';
import { useChatStore } from '@/store/chat';
import { useMcpStore } from '@/store/mcp';
import { AskDecisionBar } from './AskDecisionBar';

/**
 * FALLBACK surface for MCP tool calls the proxy is holding on this user's decision
 * (`mcp_tool_ask` targeted WS frames; 60s server-side timeout → deny). The primary decision UI
 * is the in-bubble bar on the matching transcript ToolCallCard — this stack only backstops asks
 * no visible card has claimed (e.g. after a mid-hold reload). Rendered ANCHORED ABOVE THE
 * COMPOSER (not a floating top banner) as solid cards using the SAME AskDecisionBar as the
 * bubble, so the fallback offers identical affordances (Allow/Deny with once / this-conversation
 * / always scopes) rather than a stripped-down prompt.
 *
 * Rehydrates from GET /api/mcp/ask/pending on mount, on session switch, and on every chat-WS
 * reconnect: the WS frame is fire-and-forget, so a tab that reloads — or whose socket was down
 * when the ask was minted — would otherwise never see it.
 */
export function McpAskNotice({ sessionId }: { sessionId?: string }) {
  const pendingAsks = useMcpStore((s) => s.pendingAsks);
  const claimedAsks = useMcpStore((s) => s.claimedAsks);
  const setAsks = useMcpStore((s) => s.setAsks);
  // Chat-socket state: an ask minted while the socket was down never sent its frame
  // to this tab, so every reconnect (rising edge) must rehydrate too — otherwise the
  // hold silently times out to deny with no prompt ever shown. The effect below also
  // re-runs on the falling edge but skips the fetch (pointless during an outage).
  const wsConnected = useChatStore((s) => s.isConnected);

  // The store is global but asks are per-conversation: clear it when the session actually
  // CHANGES (not on first mount — a WS frame may already have landed), then rehydrate from
  // the server and MERGE (a live frame can race the fetch; addAsk dedups by ask_id).
  // Re-runs on WS reconnect (wsConnected dep) to pick up asks missed during the blip.
  const prevSession = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (prevSession.current !== undefined && prevSession.current !== sessionId) {
      setAsks([]);
    }
    prevSession.current = sessionId;
    // Fetch only while the socket is up: a falling-edge run would just burn the
    // HTTP retry budget mid-outage; the rising edge re-runs this effect anyway.
    if (!sessionId || !wsConnected) return;
    let stale = false;
    getPendingMcpAsks(sessionId)
      .then((asks) => {
        if (stale) return;
        const add = useMcpStore.getState().addAsk;
        asks.forEach((a) =>
          add({ askId: a.ask_id, mcpKey: a.mcp_key, tool: a.tool, sessionId: a.session_id }),
        );
      })
      .catch(() => {
        /* rehydrate is best-effort; live WS frames still arrive */
      });
    return () => {
      stale = true;
    };
  }, [sessionId, setAsks, wsConnected]);

  // Asks claimed by a transcript tool card render the bar in the bubble itself; this stack only
  // backstops asks with no visible card (e.g. after a mid-hold reload).
  const visibleAsks = pendingAsks.filter((a) => !claimedAsks[a.askId]);
  if (visibleAsks.length === 0) return null;

  return (
    <div data-testid="mcp-ask-fallback" className="mb-2 space-y-1">
      {visibleAsks.map((ask) => (
        <AskDecisionBar
          key={ask.askId}
          ask={ask}
          className="rounded-lg border border-blue-600/50 bg-gray-800 shadow-lg"
        />
      ))}
    </div>
  );
}
