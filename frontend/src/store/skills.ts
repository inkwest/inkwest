import { create } from 'zustand';
import { Skill } from '@/types/api';
import * as api from '@/services/api';

// Chat-side skill list: the user's EFFECTIVE set (admin minus disabled, overlaid by
// personal), i.e. exactly what the sandbox materializes. The admin catalog lives in
// store/admin.ts and is only for the settings/admin editors.
interface SkillsState {
  templates: Skill[];
  isLoading: boolean;
  error: string | null;

  fetchTemplates: () => Promise<void>;
}

// Guards overlapping fetches (Settings writes fire one each — a rapid disable/enable
// overlaps two): only the newest fetch may write, else the slower, older snapshot
// lands last and resurrects the pre-edit list.
let fetchSeq = 0;

export const useSkillsStore = create<SkillsState>((set) => ({
  templates: [],
  isLoading: false,
  error: null,

  fetchTemplates: async () => {
    const seq = ++fetchSeq;
    set({ isLoading: true, error: null });
    try {
      const templates = await api.getEffectiveSkills();
      if (seq !== fetchSeq) return; // superseded by a newer fetch
      set({ templates: Array.isArray(templates) ? templates : [], isLoading: false });
    } catch (error) {
      if (seq !== fetchSeq) return;
      // Keep the last-good list: this also runs as a background refresh after a Settings
      // write, where blanking the picker on a transient failure loses skills the user can
      // still trigger (the sandbox reads the DB, not this store).
      set({ error: (error as Error).message, isLoading: false });
    }
  },
}));
