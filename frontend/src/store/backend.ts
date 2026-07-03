import { create } from 'zustand';

// Tracks whether the backend is currently unreachable.
//
// AWS/ALB answers 502/503/504 (and the network layer fails outright) when the
// FastAPI backend is restarting (deploy, OOM, crash-loop) or briefly gone. While
// any HTTP call is actively backing off, the banner should show; once a call gets
// a real response from the app — or a WebSocket re-opens — it should clear.
//
// `unavailable` is derived from a REFCOUNT of in-flight retrying calls rather than
// a plain boolean: with many concurrent calls, a fast read resolving must not flip
// the banner off while a slow call is still mid-backoff (and vice-versa). The
// banner is shown iff at least one call is currently retrying. `reachable()` is the
// ground-truth hard-clear: a response that actually came from the app, or a WS
// onopen, proves the backend is up, so it zeroes the count regardless.
// One deduped 5xx bucket: the backend answered (reachable) but the endpoint is
// broken server-side. Unlike the retry/unavailable machinery these must be
// SURFACED, not absorbed — a caller's best-effort `catch {}` otherwise hides a
// real backend bug (e.g. a missed migration 500ing every poll) indefinitely.
interface ServerErrorEntry {
  status: number;
  message: string;
  count: number;
}

interface BackendState {
  unavailable: boolean;
  retrying: number;
  // A WS reconnect loop exhausted its wall-clock budget — the backend looks
  // permanently gone, not just bouncing. The banner switches to a reload prompt.
  gaveUp: boolean;
  // 5xx responses keyed by "METHOD /path" (deduped; count bumps on repeats).
  serverErrors: Record<string, ServerErrorEntry>;
  // A call entered its backoff loop (backend looks down).
  beginRetry: () => void;
  // A call left its backoff loop (resolved, gave up, or aborted).
  endRetry: () => void;
  // Proof the backend is reachable (a real app response / a WS reconnect): clear.
  reachable: () => void;
  // A reconnect loop gave up after its budget — surface the manual-reload prompt.
  markGaveUp: () => void;
  // A request got a 5xx from the app itself — record it for the banner.
  reportServerError: (key: string, status: number, message: string) => void;
  // The same endpoint answered fine again (deploy finished, bug fixed): self-heal
  // its strip entry instead of leaving a stale red alert until manual dismiss.
  clearServerError: (key: string) => void;
  // User acknowledged the errors; a NEW failure re-surfaces the strip.
  dismissServerErrors: () => void;
}

// Bound the strip: a systemic outage (DB down, app up) can 500 many distinct
// endpoints; beyond this the oldest entries are evicted — the survivors plus the
// per-key counts still tell the story without growing the store/DOM unboundedly.
export const MAX_SERVER_ERRORS = 8;

export const useBackendStore = create<BackendState>((set, get) => ({
  unavailable: false,
  retrying: 0,
  gaveUp: false,
  serverErrors: {},
  beginRetry: () => set((s) => ({ retrying: s.retrying + 1, unavailable: true })),
  endRetry: () =>
    set((s) => {
      const retrying = Math.max(0, s.retrying - 1);
      return { retrying, unavailable: retrying > 0 };
    }),
  // Guard the no-op so a stream of successful responses doesn't churn subscribers.
  // Deliberately does NOT clear `gaveUp`: once a reconnect loop has stopped it is
  // dead until reload, so an HTTP call succeeding again (backend recovered) must
  // not hide the reload prompt while the WS sockets stay down. `gaveUp` clears
  // only on a full page reload.
  reachable: () => {
    if (get().unavailable || get().retrying > 0) set({ retrying: 0, unavailable: false });
  },
  markGaveUp: () => {
    if (!get().gaveUp) set({ gaveUp: true });
  },
  reportServerError: (key, status, message) =>
    set((s) => {
      const prev = s.serverErrors[key];
      const serverErrors = {
        ...s.serverErrors,
        [key]: { status, message, count: (prev?.count ?? 0) + 1 },
      };
      // Evict oldest (insertion order) once over the cap; a repeat bump keeps its slot.
      const keys = Object.keys(serverErrors);
      for (const k of keys.slice(0, Math.max(0, keys.length - MAX_SERVER_ERRORS))) {
        delete serverErrors[k];
      }
      return { serverErrors };
    }),
  clearServerError: (key) =>
    set((s) => {
      if (!(key in s.serverErrors)) return s; // no-op guard: don't churn subscribers
      const serverErrors = { ...s.serverErrors };
      delete serverErrors[key];
      return { serverErrors };
    }),
  dismissServerErrors: () => set({ serverErrors: {} }),
}));
