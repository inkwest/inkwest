import { useEffect, useRef, useState } from 'react';
import { decideMcpAsk, getPendingMcpAsks } from '@/services/api';
import { useMcpStore } from '@/store/mcp';

/**
 * Stacked in-conversation banners for MCP tool calls the proxy is holding on this user's
 * decision (`mcp_tool_ask` targeted WS frames; 60s server-side timeout → deny). Mirrors
 * McpInlineNotice. Shows the tool name only — full tool input is already visible in the
 * transcript's tool card, so the banner deliberately carries no args.
 *
 * On mount / session switch it rehydrates from GET /api/mcp/ask/pending: the WS frame is
 * fire-and-forget, so a tab that reloads mid-hold would otherwise never see the banner.
 */
export function McpAskNotice({ sessionId }: { sessionId?: string }) {
  const pendingAsks = useMcpStore((s) => s.pendingAsks);
  const claimedAsks = useMcpStore((s) => s.claimedAsks);
  const removeAsk = useMcpStore((s) => s.removeAsk);
  const setAsks = useMcpStore((s) => s.setAsks);
  // Ask ids with an in-flight decision POST, so a double-click can't fire twice.
  const [deciding, setDeciding] = useState<Set<string>>(new Set());

  // The store is global but asks are per-conversation: clear it when the session actually
  // CHANGES (not on first mount — a WS frame may already have landed), then rehydrate from
  // the server and MERGE (a live frame can race the fetch; addAsk dedups by ask_id).
  const prevSession = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (prevSession.current !== undefined && prevSession.current !== sessionId) {
      setAsks([]);
    }
    prevSession.current = sessionId;
    if (!sessionId) return;
    let stale = false;
    getPendingMcpAsks(sessionId)
      .then((asks) => {
        if (stale) return;
        const add = useMcpStore.getState().addAsk;
        asks.forEach((a) => add({ askId: a.ask_id, mcpKey: a.mcp_key, tool: a.tool }));
      })
      .catch(() => {
        /* rehydrate is best-effort; live WS frames still arrive */
      });
    return () => {
      stale = true;
    };
  }, [sessionId, setAsks]);

  // Asks claimed by a transcript tool card render Allow/Deny in the bubble itself; the
  // banner only backstops asks with no visible card (e.g. after a mid-hold reload).
  const visibleAsks = pendingAsks.filter((a) => !claimedAsks[a.askId]);
  if (visibleAsks.length === 0) return null;

  const decide = async (askId: string, verdict: 'allow' | 'deny') => {
    if (deciding.has(askId)) return;
    setDeciding((s) => new Set(s).add(askId));
    try {
      await decideMcpAsk(askId, verdict);
    } catch {
      // 404 = already resolved/timed out; anything else — the hold will time out to
      // deny on its own. Either way the banner is dead weight now.
    } finally {
      removeAsk(askId);
      setDeciding((s) => {
        const next = new Set(s);
        next.delete(askId);
        return next;
      });
    }
  };

  return (
    <div className="sticky top-0 z-10 mb-3 space-y-2">
      {visibleAsks.map((ask) => (
        <div
          key={ask.askId}
          className="flex items-center justify-between gap-3 rounded-md border border-blue-700/50 bg-blue-900/30 px-3 py-2 text-sm text-blue-200"
        >
          <span>
            MCP server <strong>{ask.mcpKey}</strong> wants to call tool <strong>{ask.tool}</strong>.
          </span>
          <span className="flex shrink-0 gap-2">
            <button
              onClick={() => decide(ask.askId, 'allow')}
              disabled={deciding.has(ask.askId)}
              className="rounded bg-blue-700/60 px-2 py-1 text-xs text-blue-50 hover:bg-blue-700 disabled:opacity-50"
            >
              Allow
            </button>
            <button
              onClick={() => decide(ask.askId, 'deny')}
              disabled={deciding.has(ask.askId)}
              className="rounded bg-zinc-700/60 px-2 py-1 text-xs text-zinc-100 hover:bg-zinc-700 disabled:opacity-50"
            >
              Deny
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}
