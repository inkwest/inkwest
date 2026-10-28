import { create } from 'zustand';
import { getAnnouncements } from '@/services/api';
import type { Announcement } from '@/types/api';

interface AnnouncementsState {
  items: Announcement[];
  // False until a fetch has answered: "nothing yet" is not "nothing" (dismissal pruning waits on it).
  loaded: boolean;
  // Never throws: a failure keeps the previous items so a backend blip does not flicker them away.
  refresh: () => Promise<void>;
}

// Overlapping refreshes (broadcast + reconnect) may answer out of order.
let lastStarted = 0;
let lastApplied = 0;

export const useAnnouncementsStore = create<AnnouncementsState>((set) => ({
  items: [],
  loaded: false,

  refresh: async () => {
    const seq = ++lastStarted;
    try {
      const items = await getAnnouncements();
      if (!Array.isArray(items) || seq < lastApplied) return;
      lastApplied = seq;
      set({ items, loaded: true });
    } catch {
      // 004-AC11: keep what is shown.
    }
  },
}));
