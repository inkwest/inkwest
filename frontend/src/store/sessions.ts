import { create } from 'zustand';
import { SearchResponse, Session, SessionWithMessages } from '@/types/api';
import * as api from '@/services/api';

interface SessionsState {
  sessions: Session[];
  currentSession: SessionWithMessages | null;
  isLoading: boolean;
  isLoadingSession: boolean;
  error: string | null;
  pendingPrompt: string | null;
  searchResults: SearchResponse | null;
  isSearching: boolean;

  fetchSessions: () => Promise<void>;
  fetchSession: (id: string) => Promise<void>;
  createSession: (
    clusterId?: string,
    opts?: { systemPromptId?: string; systemPromptCustom?: string }
  ) => Promise<Session>;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  setCurrentSession: (session: SessionWithMessages | null) => void;
  moveSession: (fromId: string, toId: string) => void;
  persistSessionOrder: () => Promise<void>;
  setPendingPrompt: (prompt: string | null) => void;
  consumePendingPrompt: () => string | null;
  updateSessionTokens: (id: string, inputTokens: number, outputTokens: number) => void;
  searchGlobal: (query: string) => Promise<void>;
  clearSearch: () => void;
}

export const useSessionsStore = create<SessionsState>((set, get) => ({
  sessions: [],
  currentSession: null,
  isLoading: false,
  isLoadingSession: false,
  error: null,
  pendingPrompt: null,
  searchResults: null,
  isSearching: false,

  fetchSessions: async () => {
    const isInitialLoad = get().sessions.length === 0;
    if (isInitialLoad) {
      set({ isLoading: true, error: null });
    }
    try {
      const sessions = await api.getSessions();
      set({ sessions: Array.isArray(sessions) ? sessions : [], isLoading: false });
    } catch (error) {
      if (isInitialLoad) {
        set({ error: (error as Error).message, isLoading: false, sessions: [] });
      }
    }
  },

  fetchSession: async (id: string) => {
    const isInitialLoad = get().currentSession?.id !== id;
    if (isInitialLoad) {
      set({ isLoadingSession: true, error: null });
    }
    try {
      const session = await api.getSession(id);
      set({ currentSession: session, isLoadingSession: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoadingSession: false });
    }
  },

  createSession: async (clusterId?: string, opts?: { systemPromptId?: string; systemPromptCustom?: string }) => {
    const session = await api.createSession({
      ...(clusterId ? { cluster_id: clusterId } : {}),
      ...(opts?.systemPromptId ? { system_prompt_id: opts.systemPromptId } : {}),
      ...(opts?.systemPromptCustom ? { system_prompt_custom: opts.systemPromptCustom } : {}),
    });
    set((state) => ({ sessions: [session, ...state.sessions] }));
    return session;
  },

  deleteSession: async (id: string) => {
    await api.deleteSession(id);
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSession: state.currentSession?.id === id ? null : state.currentSession,
    }));
  },

  renameSession: async (id: string, title: string) => {
    const updated = await api.updateSession(id, { title });
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, title: updated.title } : s)),
      currentSession: state.currentSession?.id === id
        ? { ...state.currentSession, title: updated.title }
        : state.currentSession,
    }));
  },

  setCurrentSession: (session) => set({ currentSession: session }),

  // Live, optimistic reorder during a drag (no network). moveSession places the
  // dragged session into the target's slot; persistSessionOrder saves the result.
  moveSession: (fromId, toId) =>
    set((state) => {
      const from = state.sessions.findIndex((s) => s.id === fromId);
      const to = state.sessions.findIndex((s) => s.id === toId);
      if (from === -1 || to === -1 || from === to) return state;
      const next = state.sessions.slice();
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return { sessions: next };
    }),

  persistSessionOrder: async () => {
    const ids = get().sessions.map((s) => s.id);
    try {
      await api.reorderSessions(ids);
    } catch {
      // Couldn't save the order — re-sync from the server rather than leave the
      // UI showing an arrangement the backend doesn't have.
      get().fetchSessions();
    }
  },

  setPendingPrompt: (prompt) => set({ pendingPrompt: prompt }),

  consumePendingPrompt: () => {
    const prompt = get().pendingPrompt;
    if (prompt) {
      set({ pendingPrompt: null });
    }
    return prompt;
  },

  searchGlobal: async (query: string) => {
    set({ isSearching: true });
    try {
      const results = await api.searchGlobal(query);
      set({ searchResults: results, isSearching: false });
    } catch {
      set({ searchResults: null, isSearching: false });
    }
  },

  updateSessionTokens: (id, inputTokens, outputTokens) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, total_input_tokens: inputTokens, total_output_tokens: outputTokens } : s,
      ),
      currentSession:
        state.currentSession?.id === id
          ? { ...state.currentSession, total_input_tokens: inputTokens, total_output_tokens: outputTokens }
          : state.currentSession,
    }));
  },

  clearSearch: () => set({ searchResults: null, isSearching: false }),
}));
