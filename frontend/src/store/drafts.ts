import { create } from 'zustand';
import { persist } from 'zustand/middleware';

// Per-session unsent prompt drafts. Kept OUTSIDE the chat store (which is reset
// on every session switch) and persisted to localStorage so a draft survives
// both navigating between sessions and a full page reload.
interface DraftsState {
  drafts: Record<string, string>;
  setDraft: (sessionId: string, text: string) => void;
  clearDraft: (sessionId: string) => void;
}

export const useDraftsStore = create<DraftsState>()(
  persist(
    (set) => ({
      drafts: {},
      setDraft: (sessionId, text) =>
        set((state) => ({ drafts: { ...state.drafts, [sessionId]: text } })),
      clearDraft: (sessionId) =>
        set((state) => {
          if (!(sessionId in state.drafts)) return state;
          const next = { ...state.drafts };
          delete next[sessionId];
          return { drafts: next };
        }),
    }),
    { name: 'inkwest-drafts' }
  )
);
