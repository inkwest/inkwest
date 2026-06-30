import { create } from 'zustand';

/**
 * OAuth2 MCP servers the backend flagged as needing a reconnect (an `mcp_auth_required`
 * chat event — an enabled server whose grant is missing/expired/revoked). Drives the
 * in-conversation reconnect banner (McpInlineNotice). Global (not per-session): the grant
 * is per-user, and the banner clears once the key reconnects.
 */
interface McpState {
  authRequired: string[];
  flagAuthRequired: (key: string) => void;
  clearAuthRequired: (key: string) => void;
  // Enabled MCP server keys for the conversation whose composer is currently mounted,
  // mirrored from McpPanel (its load/apply/disconnect writes) so other surfaces — e.g.
  // ChatInput's "no connector activated" warning — can reactively tell whether the
  // session has any MCP access without their own fetch.
  enabledBySession: Record<string, string[]>;
  setSessionEnabled: (sessionId: string, keys: string[]) => void;
}

export const useMcpStore = create<McpState>((set) => ({
  authRequired: [],
  flagAuthRequired: (key) =>
    set((s) => (s.authRequired.includes(key) ? s : { authRequired: [...s.authRequired, key] })),
  clearAuthRequired: (key) =>
    set((s) => ({ authRequired: s.authRequired.filter((k) => k !== key) })),
  enabledBySession: {},
  // Replace the whole map (not merge): only one conversation's composer is mounted at a
  // time, so a single entry is all that's ever read — this bounds the map instead of
  // accumulating a key per visited session for the tab's lifetime.
  setSessionEnabled: (sessionId, keys) => set({ enabledBySession: { [sessionId]: keys } }),
}));
