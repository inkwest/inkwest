import { create } from 'zustand';
import { AppConfig, getAppConfig } from '@/services/api';

interface ConfigState {
  config: AppConfig | null;
  fetchConfig: () => Promise<void>;
}

export const useConfigStore = create<ConfigState>()((set, get) => ({
  config: null,

  fetchConfig: async () => {
    if (get().config) return; // already loaded
    try {
      set({ config: await getAppConfig() });
    } catch {
      // Purely informational (model badge) — stay hidden on failure.
    }
  },
}));
