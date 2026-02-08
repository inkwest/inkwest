import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type TaskListPosition = 'hidden' | 'top' | 'bottom';

interface SettingsState {
  compactToolCards: boolean;
  taskListPosition: TaskListPosition;
  sidebarOpen: boolean;
  sidebarWidth: number;

  setCompactToolCards: (compact: boolean) => void;
  toggleCompactToolCards: () => void;
  setTaskListPosition: (position: TaskListPosition) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setSidebarWidth: (width: number) => void;
}

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      compactToolCards: true,
      taskListPosition: 'bottom',
      sidebarOpen: false,
      sidebarWidth: 256,

      setCompactToolCards: (compact) => set({ compactToolCards: compact }),
      toggleCompactToolCards: () =>
        set((state) => ({ compactToolCards: !state.compactToolCards })),
      setTaskListPosition: (position) => set({ taskListPosition: position }),
      toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
      setSidebarOpen: (open) => set({ sidebarOpen: open }),
      setSidebarWidth: (width) => set({ sidebarWidth: Math.min(480, Math.max(200, width)) }),
    }),
    {
      name: 'inkwest-settings',
      partialize: (state) => ({
        compactToolCards: state.compactToolCards,
        taskListPosition: state.taskListPosition,
        sidebarWidth: state.sidebarWidth,
      }),
    }
  )
);
