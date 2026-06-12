import { create } from 'zustand';
import { Skill } from '@/types/api';
import * as api from '@/services/api';

interface SkillsState {
  templates: Skill[];
  isLoading: boolean;
  error: string | null;

  fetchTemplates: () => Promise<void>;
}

export const useSkillsStore = create<SkillsState>((set) => ({
  templates: [],
  isLoading: false,
  error: null,

  fetchTemplates: async () => {
    set({ isLoading: true, error: null });
    try {
      const templates = await api.getSkills();
      set({ templates: Array.isArray(templates) ? templates : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false, templates: [] });
    }
  },
}));
