import { create } from 'zustand';
import { PromptTemplate } from '@/types/api';
import * as api from '@/services/api';

interface PromptTemplatesState {
  templates: PromptTemplate[];
  isLoading: boolean;
  error: string | null;

  fetchTemplates: () => Promise<void>;
}

export const usePromptTemplatesStore = create<PromptTemplatesState>((set) => ({
  templates: [],
  isLoading: false,
  error: null,

  fetchTemplates: async () => {
    set({ isLoading: true, error: null });
    try {
      const templates = await api.getPromptTemplates();
      set({ templates: Array.isArray(templates) ? templates : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false, templates: [] });
    }
  },
}));
