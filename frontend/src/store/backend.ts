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
interface BackendState {
  unavailable: boolean;
  retrying: number;
  // A WS reconnect loop exhausted its wall-clock budget — the backend looks
  // permanently gone, not just bouncing. The banner switches to a reload prompt.
  gaveUp: boolean;
  // A call entered its backoff loop (backend looks down).
  beginRetry: () => void;
  // A call left its backoff loop (resolved, gave up, or aborted).
  endRetry: () => void;
  // Proof the backend is reachable (a real app response / a WS reconnect): clear.
  reachable: () => void;
  // A reconnect loop gave up after its budget — surface the manual-reload prompt.
  markGaveUp: () => void;
}

export const useBackendStore = create<BackendState>((set, get) => ({
  unavailable: false,
  retrying: 0,
  gaveUp: false,
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
}));
