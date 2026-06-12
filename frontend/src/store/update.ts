import { create } from 'zustand';

// Tracks whether the running SPA is stale relative to the deployed backend.
//
// The first version seen (at page load / first WS connect) is the baseline the
// browser is running. Every later reading comes from a WS reconnect — a deploy
// rolls pods and drops the socket, so reconnect time is when a client is most
// likely behind. A reading that differs from the baseline means a new build is
// live: flag it once and stop (the version only moves forward within a session).
interface UpdateState {
  // Version the browser loaded with; null until the first reading.
  loadedVersion: string | null;
  // A newer backend version was observed → offer a reload.
  updateAvailable: boolean;

  // Feed a freshly-read backend version: sets the baseline on the first call,
  // flags an update on any later differing call.
  recordVersion: (version: string) => void;
  // Hide the banner without forgetting the baseline; a later reconnect that
  // still sees a newer version re-surfaces it.
  dismiss: () => void;
  reset: () => void;
}

export const useUpdateStore = create<UpdateState>((set) => ({
  loadedVersion: null,
  updateAvailable: false,

  recordVersion: (version) =>
    set((s) => {
      if (s.loadedVersion === null) return { loadedVersion: version };
      if (version !== s.loadedVersion && !s.updateAvailable) return { updateAvailable: true };
      return {};
    }),

  dismiss: () => set({ updateAvailable: false }),
  reset: () => set({ loadedVersion: null, updateAvailable: false }),
}));
