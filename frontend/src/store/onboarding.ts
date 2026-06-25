import { create } from 'zustand';
import { persist } from 'zustand/middleware';

// Bump when the tour changes meaningfully. The persisted key is version-scoped, so a new
// version is "unseen" for everyone (their old key is ignored) and the tour replays
// automatically — this is how we force users back through a changed onboarding.
export const ONBOARDING_VERSION = 1;

interface OnboardingState {
  // Whether the current tour version has been seen/dismissed. Persisted per browser
  // under a version-scoped key (see below), so it's per-device, not per-user.
  completed: boolean;
  complete: () => void;
  // Replay the tour (e.g. from the "Replay tutorial" menu item) without bumping the
  // version — just clears the seen flag for the current version.
  restart: () => void;
}

export const useOnboardingStore = create<OnboardingState>()(
  persist(
    (set) => ({
      completed: false,
      complete: () => set({ completed: true }),
      restart: () => set({ completed: false }),
    }),
    { name: `inkwest-onboarding-v${ONBOARDING_VERSION}` }
  )
);
