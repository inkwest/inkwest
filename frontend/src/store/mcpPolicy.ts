import { create } from 'zustand';

// Cross-instance signal that the user's GLOBAL MCP tool policy changed, so a conversation-scope
// editor (which shows tools as INHERITED from the global defaults) can re-fetch and reflect it —
// e.g. after the stacked global Settings modal edits it above an open conversation panel.
interface McpPolicyVersionState {
  global: number;
  bumpGlobal: () => void;
}

export const useMcpPolicyVersion = create<McpPolicyVersionState>((set) => ({
  global: 0,
  bumpGlobal: () => set((s) => ({ global: s.global + 1 })),
}));
