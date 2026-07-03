import { create } from 'zustand';

// Cross-instance signal that the user's GLOBAL MCP tool policy changed, so a conversation-scope
// editor (which shows tools as INHERITED from the global defaults) can re-fetch and reflect it —
// e.g. after the stacked global Settings modal edits it above an open conversation panel.
interface McpPolicyVersionState {
  global: number;
  bumpGlobal: () => void;
  // Same signal for a CONVERSATION's per-tool policy (e.g. the in-bubble ask bar wrote
  // "deny for this conversation" while the conversation MCP popup is open).
  session: number;
  bumpSession: () => void;
}

export const useMcpPolicyVersion = create<McpPolicyVersionState>((set) => ({
  global: 0,
  bumpGlobal: () => set((s) => ({ global: s.global + 1 })),
  session: 0,
  bumpSession: () => set((s) => ({ session: s.session + 1 })),
}));
