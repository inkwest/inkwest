import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { Attachment, SearchResponse, Session, SessionWithMessages } from '@/types/api';
import * as api from '@/services/api';
import type { SessionSort, SessionOrder, ShareFilter } from '@/services/api';
import { useDraftsStore } from '@/store/drafts';

// Sidebar page size; the list starts at one page and grows via loadMoreSessions.
const PAGE_SIZE = 20;

// Monotonic token for list fetches. Rapid sort/filter changes (each fires a fetch) or a
// sync-triggered refetch racing a user toggle would otherwise be last-write-wins: the
// older multi-page loop can resolve last and overwrite state with a stale order. Each
// fetchSessions claims a new token; any resolution carrying an outdated token is dropped.
let listFetchToken = 0;

// "Last activity" date window: relative presets (persist the key, not a timestamp, so a
// saved choice stays meaningful across days) or a 'custom' absolute from–to range.
export type DateRange = 'all' | 'today' | '7d' | '30d' | 'custom';

// Resolve the active window to absolute ISO bounds, recomputed per fetch so relative
// presets stay current. 'all' → no bounds; presets → lower bound only; 'custom' → the
// from/to dates interpreted in local time (from = start of day, to = end of day).
function activityBounds(
  range: DateRange,
  customFrom: string,
  customTo: string
): { activity_after?: string; activity_before?: string } {
  if (range === 'all') return {};
  if (range === 'custom') {
    // Defensive: these come from persisted localStorage, which can be corrupted or
    // hand-edited. toISOString() throws on an Invalid Date, which would silently kill
    // every subsequent fetch — skip an unparsable bound instead.
    const bounds: { activity_after?: string; activity_before?: string } = {};
    const from = new Date(`${customFrom}T00:00:00`);
    const to = new Date(`${customTo}T23:59:59.999`);
    if (customFrom && !isNaN(from.getTime())) bounds.activity_after = from.toISOString();
    if (customTo && !isNaN(to.getTime())) bounds.activity_before = to.toISOString();
    return bounds;
  }
  const d = new Date();
  if (range === 'today') d.setHours(0, 0, 0, 0); // local midnight
  else if (range === '7d') d.setDate(d.getDate() - 7);
  else if (range === '30d') d.setDate(d.getDate() - 30);
  return { activity_after: d.toISOString() };
}

// All sharing buckets. They partition the visible set, so selecting every one is
// equivalent to no filter — the default shows all three checked and collapses to []
// on the wire (keeping the hot default path filter-free).
const SHARE_FILTER_VALUES: ShareFilter[] = ['shared_with_me', 'shared_by_me', 'private'];

// The default sidebar view. Shared by the initial store state and resetView so the
// two can't drift; the trigger's active-dot / reset button key off deviation from it.
const DEFAULT_VIEW = {
  sortMode: 'activity' as SessionSort,
  sortOrder: 'desc' as SessionOrder,
  filters: [...SHARE_FILTER_VALUES] as ShareFilter[],
  dateRange: 'all' as DateRange,
  customFrom: '',
  customTo: '',
};

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
  // Sidebar view state (sort + sharing filters), persisted to localStorage.
  sortMode: SessionSort;
  sortOrder: SessionOrder;
  filters: ShareFilter[];
  dateRange: DateRange;
  // Custom absolute window (YYYY-MM-DD, local), used only when dateRange === 'custom'.
  customFrom: string;
  customTo: string;
  // Server-side pagination: total matching the current sort/filters, and how many
  // pages are currently loaded (fetchSessions reloads all of them so a live sync
  // doesn't collapse a list the user expanded via "load more").
  total: number;
  loadedPages: number;
  isLoadingMore: boolean;
  // True once any list fetch has succeeded. Drives the "Loading..." gate: an empty
  // *result* (filter matched nothing) is not an initial load — treating it as one made
  // the whole sidebar flash "Loading..." on the next filter change.
  hasLoaded: boolean;

  fetchSessions: () => Promise<void>;
  loadMoreSessions: () => Promise<void>;
  setSortMode: (mode: SessionSort) => void;
  setSortOrder: (order: SessionOrder) => void;
  toggleFilter: (filter: ShareFilter) => void;
  setFilters: (filters: ShareFilter[]) => void;
  setDateRange: (range: DateRange) => void;
  setCustomRange: (from: string, to: string) => void;
  resetView: () => void;
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

export const useSessionsStore = create<SessionsState>()(
  persist(
    (set, get) => ({
      sessions: [],
      currentSession: null,
      isLoading: false,
      isLoadingSession: false,
      error: null,
      pendingPrompt: null,
      pendingAttachments: null,
      searchResults: null,
      isSearching: false,
      // Default view: most recently active conversations first. A persisted user
      // choice (localStorage) overrides this on subsequent loads.
      ...DEFAULT_VIEW,
      total: 0,
      loadedPages: 1,
      isLoadingMore: false,
      hasLoaded: false,

      // Reload every page currently shown (loadedPages), preserving a "load more"
      // expansion across a live sync / manual refresh. Sequential fetches aren't a
      // consistent snapshot, so dedupe by id in case a session shifts between pages.
      fetchSessions: async () => {
        const { hasLoaded, sortMode, sortOrder, filters, dateRange, customFrom, customTo, loadedPages } =
          get();
        const token = ++listFetchToken;
        // Only a true first load gates the sidebar behind "Loading..." — a refetch whose
        // *current result* happens to be empty (filter matched nothing) must swap in
        // place, or every filter change from an empty list flashes the whole sidebar.
        const isInitialLoad = !hasLoaded;
        if (isInitialLoad) {
          set({ isLoading: true, error: null });
        }
        try {
          const acc: Session[] = [];
          const seen = new Set<string>();
          const dateBounds = activityBounds(dateRange, customFrom, customTo);
          // All buckets selected ≡ no filter — send [] so the default path stays cheap.
          const share = filters.length === SHARE_FILTER_VALUES.length ? [] : filters;
          let total = 0;
          for (let page = 1; page <= Math.max(1, loadedPages); page++) {
            const resp = await api.getSessions({
              page,
              page_size: PAGE_SIZE,
              sort: sortMode,
              order: sortOrder,
              share,
              ...dateBounds,
            });
            total = resp.total;
            for (const s of resp.items) {
              if (!seen.has(s.id)) {
                seen.add(s.id);
                acc.push(s);
              }
            }
            if (acc.length >= total) break; // fetched everything that exists
          }
          if (token !== listFetchToken) return; // superseded by a newer fetch — drop
          set({ sessions: acc, total, isLoading: false, hasLoaded: true });
        } catch (error) {
          if (isInitialLoad && token === listFetchToken) {
            set({ error: (error as Error).message, isLoading: false, sessions: [] });
          }
        }
      },

      loadMoreSessions: async () => {
        const { sessions, total, sortMode, sortOrder, filters, dateRange, customFrom, customTo, loadedPages, isLoadingMore } =
          get();
        if (isLoadingMore || sessions.length >= total) return;
        // Extends the CURRENT view: capture (don't bump) the token so a sort/filter
        // change mid-flight supersedes this append instead of merging stale rows.
        const token = listFetchToken;
        set({ isLoadingMore: true });
        try {
          const nextPage = loadedPages + 1;
          const dateBounds = activityBounds(dateRange, customFrom, customTo);
          const share = filters.length === SHARE_FILTER_VALUES.length ? [] : filters;
          const resp = await api.getSessions({
            page: nextPage,
            page_size: PAGE_SIZE,
            sort: sortMode,
            order: sortOrder,
            share,
            ...dateBounds,
          });
          if (token !== listFetchToken) {
            set({ isLoadingMore: false });
            return;
          }
          set((state) => {
            const seen = new Set(state.sessions.map((s) => s.id));
            const fresh = resp.items.filter((s) => !seen.has(s.id));
            return {
              sessions: [...state.sessions, ...fresh],
              total: resp.total,
              loadedPages: nextPage,
              isLoadingMore: false,
            };
          });
        } catch {
          set({ isLoadingMore: false });
        }
      },

      // Changing sort/filter is a fresh view: reset to the first page, then refetch.
      setSortMode: (mode) => {
        if (get().sortMode === mode) return;
        set({ sortMode: mode, loadedPages: 1 });
        get().fetchSessions();
      },
      setSortOrder: (order) => {
        if (get().sortOrder === order) return;
        set({ sortOrder: order, loadedPages: 1 });
        get().fetchSessions();
      },
      toggleFilter: (filter) => {
        set((state) => ({
          filters: state.filters.includes(filter)
            ? state.filters.filter((f) => f !== filter)
            : [...state.filters, filter],
          loadedPages: 1,
        }));
        get().fetchSessions();
      },
      // Replace the whole sharing-filter set at once (drives the "Show all" select-all
      // toggle: [] = show everything, all three = the same result, explicitly checked).
      setFilters: (next) => {
        set({ filters: next, loadedPages: 1 });
        get().fetchSessions();
      },
      setDateRange: (range) => {
        if (get().dateRange === range) return;
        set({ dateRange: range, loadedPages: 1 });
        get().fetchSessions();
      },
      // Set the custom from–to window (empty string = open-ended on that side) and
      // switch to the custom range in one go.
      setCustomRange: (from, to) => {
        set({ dateRange: 'custom', customFrom: from, customTo: to, loadedPages: 1 });
        get().fetchSessions();
      },
      // Restore the default view (latest activity, desc, all buckets shown, all time).
      resetView: () => {
        set({ ...DEFAULT_VIEW, filters: [...SHARE_FILTER_VALUES], loadedPages: 1 });
        get().fetchSessions();
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
          total: Math.max(0, state.total - 1),
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
          total: Math.max(0, state.total - 1),
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
    }),
    {
      name: 'inkwest-session-view',
      version: 1,
      // v0 stored the "show everything" default as an empty filter list; v1 represents
      // it as all buckets selected so the checkboxes reflect it. Coerce a legacy empty
      // set on rehydrate; a persisted proper subset is left untouched.
      migrate: (persisted) => {
        const s = (persisted ?? {}) as Partial<SessionsState>;
        if (!Array.isArray(s.filters) || s.filters.length === 0) {
          s.filters = [...SHARE_FILTER_VALUES];
        }
        return s as SessionsState;
      },
      // Persist only the user's view preferences — never the session data itself.
      partialize: (state) => ({
        sortMode: state.sortMode,
        sortOrder: state.sortOrder,
        filters: state.filters,
        dateRange: state.dateRange,
        customFrom: state.customFrom,
        customTo: state.customTo,
      }),
    }
  )
);
