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
}

export const useMcpStore = create<McpState>((set) => ({
  authRequired: [],
  flagAuthRequired: (key) =>
    set((s) => (s.authRequired.includes(key) ? s : { authRequired: [...s.authRequired, key] })),
  clearAuthRequired: (key) =>
    set((s) => ({ authRequired: s.authRequired.filter((k) => k !== key) })),
}));
