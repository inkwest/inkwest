import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface SettingsState {
  compactToolCards: boolean;
  showTimestamps: boolean;
  // Per-bubble token line (in/out/cache splits) next to the timestamp.
  showTokenCounts: boolean;
  // Composer width in chat: false = comfortable reading width, true = full width.
  wideComposer: boolean;
  sidebarOpen: boolean;
  sidebarWidth: number;

  setCompactToolCards: (compact: boolean) => void;
  toggleCompactToolCards: () => void;
  setShowTimestamps: (show: boolean) => void;
  toggleShowTimestamps: () => void;
  setShowTokenCounts: (show: boolean) => void;
  toggleWideComposer: () => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setSidebarWidth: (width: number) => void;
}

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      compactToolCards: true,
      showTimestamps: true,
      showTokenCounts: true,
      wideComposer: false,
      sidebarOpen: false,
      sidebarWidth: 256,

      setCompactToolCards: (compact) => set({ compactToolCards: compact }),
      toggleCompactToolCards: () =>
        set((state) => ({ compactToolCards: !state.compactToolCards })),
      setShowTimestamps: (show) => set({ showTimestamps: show }),
      toggleShowTimestamps: () =>
        set((state) => ({ showTimestamps: !state.showTimestamps })),
      setShowTokenCounts: (show) => set({ showTokenCounts: show }),
      toggleWideComposer: () =>
        set((state) => ({ wideComposer: !state.wideComposer })),
      toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
      setSidebarOpen: (open) => set({ sidebarOpen: open }),
      setSidebarWidth: (width) => set({ sidebarWidth: Math.min(480, Math.max(200, width)) }),
    }),
    {
      name: 'inkwest-settings',
      partialize: (state) => ({
        compactToolCards: state.compactToolCards,
        showTimestamps: state.showTimestamps,
        showTokenCounts: state.showTokenCounts,
        wideComposer: state.wideComposer,
        sidebarWidth: state.sidebarWidth,
      }),
    }
  )
);
