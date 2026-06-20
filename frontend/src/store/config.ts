import { create } from 'zustand';
import { AppConfig, getAppConfig } from '@/services/api';

interface ConfigState {
  config: AppConfig | null;
  fetchConfig: () => Promise<void>;
  refreshConfig: () => Promise<void>;
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

  // Force a re-fetch (bypasses the loaded-once guard). Used after an admin model
  // save so the chat-side picker reflects the new enabled set + per-model engines,
  // which the server computes — no need to duplicate that logic client-side.
  refreshConfig: async () => {
    try {
      set({ config: await getAppConfig() });
    } catch {
      // Best-effort: keep the previous config on failure.
    }
  },
}));
