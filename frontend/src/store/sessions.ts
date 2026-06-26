import { create } from 'zustand';
import { Attachment, SearchResponse, Session, SessionWithMessages } from '@/types/api';
import * as api from '@/services/api';
import { useDraftsStore } from '@/store/drafts';

// Mirror the backend's ensure_session_title (messages.py): first 60 chars,
// breaking at a word boundary if it lands reasonably far in. Kept in sync so the
// optimistic sidebar title matches the canonical server title (no reconcile flicker).
function deriveSessionTitle(message: string): string {
  const text = message.trim();
  if (text.length <= 60) return text;
  const truncated = text.slice(0, 60);
  const lastSpace = truncated.lastIndexOf(' ');
  return (lastSpace > 40 ? truncated.slice(0, lastSpace) : truncated) + '...';
}

interface SessionsState {
  sessions: Session[];
  currentSession: SessionWithMessages | null;
  isLoading: boolean;
  isLoadingSession: boolean;
  error: string | null;
  pendingPrompt: string | null;
  // Attachments uploaded on the start screen, handed off to the real conversation's
  // auto-submit (the files live server-side under the just-created session).
  pendingAttachments: Attachment[] | null;
  searchResults: SearchResponse | null;
  isSearching: boolean;

  fetchSessions: () => Promise<void>;
  fetchSession: (id: string) => Promise<void>;
  createSession: (
    clusterId?: string,
    opts?: { systemPromptId?: string; systemPromptCustom?: string; model?: string; engine?: string }
  ) => Promise<Session>;
  deleteSession: (id: string) => Promise<void>;
  leaveSession: (id: string) => Promise<void>;
  setShareCount: (id: string, count: number) => void;
  ensureSessionTitle: (id: string, firstMessage: string) => void;
  renameSession: (id: string, title: string) => Promise<void>;
  setSessionModel: (id: string, model: string | null, engine?: string | null) => Promise<void>;
  setCurrentSession: (session: SessionWithMessages | null) => void;
  moveSession: (fromId: string, toId: string) => void;
  persistSessionOrder: () => Promise<void>;
  setPendingPrompt: (prompt: string | null, attachments?: Attachment[] | null) => void;
  consumePendingPrompt: () => string | null;
  consumePendingAttachments: () => Attachment[] | null;
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
  pendingAttachments: null,
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

  createSession: async (
    clusterId?: string,
    opts?: { systemPromptId?: string; systemPromptCustom?: string; model?: string; engine?: string }
  ) => {
    const session = await api.createSession({
      ...(clusterId ? { cluster_id: clusterId } : {}),
      ...(opts?.systemPromptId ? { system_prompt_id: opts.systemPromptId } : {}),
      ...(opts?.systemPromptCustom ? { system_prompt_custom: opts.systemPromptCustom } : {}),
      ...(opts?.model ? { model: opts.model } : {}),
      ...(opts?.engine ? { engine: opts.engine } : {}),
    });
    set((state) => ({ sessions: [session, ...state.sessions] }));
    return session;
  },

  deleteSession: async (id: string) => {
    await api.deleteSession(id);
    // Drop any orphaned draft so it can't resurface on a reused id / clutter storage.
    useDraftsStore.getState().clearDraft(id);
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSession: state.currentSession?.id === id ? null : state.currentSession,
    }));
  },

  // Keep the "Shared·N" count fresh on the open conversation when the owner manages
  // shares in the dialog (no full refetch needed — the modal already has the numbers).
  setShareCount: (id: string, count: number) =>
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, share_count: count } : s)),
      currentSession:
        state.currentSession?.id === id
          ? { ...state.currentSession, share_count: count }
          : state.currentSession,
    })),

  // Leave a conversation shared with you: removes your own access (owner's copy stays).
  // Mirrors deleteSession's local cleanup; the participant is not the owner.
  leaveSession: async (id: string) => {
    await api.leaveShare(id);
    useDraftsStore.getState().clearDraft(id);
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== id),
      currentSession: state.currentSession?.id === id ? null : state.currentSession,
    }));
  },

  // Optimistically title a brand-new conversation from its first user message so it
  // surfaces in the sidebar immediately (the sidebar hides title-less sessions). The
  // server's title-set NOTIFY can be missed when the sync socket hasn't subscribed yet
  // as the first turn starts; this is independent of that round-trip. No-op once titled
  // (mirrors ensure_session_title) — a later fetchSessions reconciles the canonical title.
  ensureSessionTitle: (id, firstMessage) =>
    set((state) => {
      const target = state.sessions.find((s) => s.id === id);
      if (!target || target.title) return state;
      const title = deriveSessionTitle(firstMessage);
      if (!title) return state;
      return {
        sessions: state.sessions.map((s) => (s.id === id ? { ...s, title } : s)),
        currentSession:
          state.currentSession?.id === id
            ? { ...state.currentSession, title }
            : state.currentSession,
      };
    }),

  renameSession: async (id: string, title: string) => {
    const updated = await api.updateSession(id, { title });
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, title: updated.title } : s)),
      currentSession: state.currentSession?.id === id
        ? { ...state.currentSession, title: updated.title }
        : state.currentSession,
    }));
  },

  // model: null un-pins the session (follow the global default again). engine is
  // the chosen Engine·Model pairing; null = resolve per turn. Both move
  // together — the picker emits a pair — so a model change always (re)sets engine.
  setSessionModel: async (id: string, model: string | null, engine: string | null = null) => {
    const updated = await api.updateSession(id, { model, engine });
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, model: updated.model, engine: updated.engine } : s
      ),
      currentSession: state.currentSession?.id === id
        ? { ...state.currentSession, model: updated.model, engine: updated.engine }
        : state.currentSession,
    }));
    // A model switch persists a "Model changed" event server-side; reload the open
    // conversation so it appears inline (flicker-free for the already-open session).
    if (get().currentSession?.id === id) {
      await get().fetchSession(id);
    }
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

  setPendingPrompt: (prompt, attachments = null) =>
    set({ pendingPrompt: prompt, pendingAttachments: attachments }),

  consumePendingPrompt: () => {
    const prompt = get().pendingPrompt;
    if (prompt) {
      set({ pendingPrompt: null });
    }
    return prompt;
  },

  consumePendingAttachments: () => {
    const attachments = get().pendingAttachments;
    if (attachments) set({ pendingAttachments: null });
    return attachments;
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
