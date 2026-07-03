import { create } from 'zustand';
import { decideMcpAsk } from '@/services/api';

/**
 * OAuth2 MCP servers the backend flagged as needing a reconnect (an `mcp_auth_required`
 * chat event — an enabled server whose grant is missing/expired/revoked). Drives the
 * in-conversation reconnect banner (McpInlineNotice). Global (not per-session): the grant
 * is per-user, and the banner clears once the key reconnects.
 */
// A held MCP tool call awaiting the user's allow/deny (runtime "ask" gate). Fed by the
// targeted `mcp_tool_ask` WS frame plus the pending-asks rehydrate fetch on mount; drives
// the stacked McpAskNotice banners. Server-side each ask times out to deny after 60s.
export interface PendingMcpAsk {
  askId: string;
  mcpKey: string;
  tool: string;
  // The conversation the held call belongs to (from the ask frame / rehydrate).
  // Scope-"conversation" policy writes MUST target this, not the currently selected
  // session — the user may switch sessions while the call is held.
  sessionId: string;
}

interface McpState {
  authRequired: string[];
  flagAuthRequired: (key: string) => void;
  clearAuthRequired: (key: string) => void;
  pendingAsks: PendingMcpAsk[];
  addAsk: (ask: PendingMcpAsk) => void;
  removeAsk: (askId: string) => void;
  setAsks: (asks: PendingMcpAsk[]) => void;
  // Resolve a held ask — shared by the in-bubble decision bar and the fallback
  // banner so the 404/timeout semantics can't drift between the two surfaces.
  decideAsk: (askId: string, verdict: 'allow' | 'deny') => Promise<void>;
  // Ask ids currently rendered inline by a transcript tool card. The fallback banner
  // (McpAskNotice) skips claimed asks so the decision UI shows in exactly one place —
  // the bubble when the running tool card is on screen, the banner otherwise (e.g.
  // after a mid-hold reload where the transcript lost the in-flight card).
  claimedAsks: Record<string, true>;
  claimAsk: (askId: string) => void;
  releaseAsk: (askId: string) => void;
  // Enabled MCP server keys for the conversation whose composer is currently mounted,
  // mirrored from McpPanel (its load/apply/disconnect writes) so other surfaces — e.g.
  // ChatInput's "no connector activated" warning — can reactively tell whether the
  // session has any MCP access without their own fetch.
  enabledBySession: Record<string, string[]>;
  setSessionEnabled: (sessionId: string, keys: string[]) => void;
  // Bumped on an `mcp_catalog_changed` WS broadcast (an admin added/removed/reconfigured an
  // OAuth MCP server). McpPanel watches it to re-load the (already reset) catalog cache live —
  // the push half of push + revalidate-on-mount. Global: the *set* of servers is not per-user.
  catalogVersion: number;
  bumpCatalogVersion: () => void;
}

export const useMcpStore = create<McpState>((set) => ({
  authRequired: [],
  flagAuthRequired: (key) =>
    set((s) => (s.authRequired.includes(key) ? s : { authRequired: [...s.authRequired, key] })),
  clearAuthRequired: (key) =>
    set((s) => ({ authRequired: s.authRequired.filter((k) => k !== key) })),
  pendingAsks: [],
  addAsk: (ask) =>
    set((s) =>
      s.pendingAsks.some((a) => a.askId === ask.askId) ? s : { pendingAsks: [...s.pendingAsks, ask] }
    ),
  removeAsk: (askId) =>
    set((s) => {
      const claimedAsks = { ...s.claimedAsks };
      delete claimedAsks[askId];
      return { pendingAsks: s.pendingAsks.filter((a) => a.askId !== askId), claimedAsks };
    }),
  setAsks: (asks) => set({ pendingAsks: asks }),
  decideAsk: async (askId, verdict) => {
    try {
      await decideMcpAsk(askId, verdict);
    } catch {
      // 404 = already resolved/timed out; anything else — the hold times out to deny
      // server-side (fail-closed). A 5xx surfaces via the central banner strip and a
      // network failure via the amber reconnecting banner, so no extra UI here.
    } finally {
      useMcpStore.getState().removeAsk(askId);
    }
  },
  claimedAsks: {},
  claimAsk: (askId) =>
    set((s) => (s.claimedAsks[askId] ? s : { claimedAsks: { ...s.claimedAsks, [askId]: true } })),
  releaseAsk: (askId) =>
    set((s) => {
      if (!s.claimedAsks[askId]) return s;
      const claimedAsks = { ...s.claimedAsks };
      delete claimedAsks[askId];
      return { claimedAsks };
    }),
  enabledBySession: {},
  // Replace the whole map (not merge): only one conversation's composer is mounted at a
  // time, so a single entry is all that's ever read — this bounds the map instead of
  // accumulating a key per visited session for the tab's lifetime.
  setSessionEnabled: (sessionId, keys) => set({ enabledBySession: { [sessionId]: keys } }),
  catalogVersion: 0,
  bumpCatalogVersion: () => set((s) => ({ catalogVersion: s.catalogVersion + 1 })),
}));
