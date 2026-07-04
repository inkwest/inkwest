import { create } from 'zustand';
import {
  ConfigExport,
  PreferencesDocument,
  UiPreferences,
  UserSkill,
} from '@/types/api';
import {
  createMySkill,
  deleteMySkill,
  exportMyConfig,
  getMyPreferences,
  importMyConfig,
  overrideAdminSkill,
  updateMyPreferences,
  updateMySkill,
} from '@/services/api';
import { useSettingsStore } from '@/store/settings';

/**
 * DB-backed user preferences.
 *
 * The DB is the source of truth; `store/settings` stays the live, synchronous UI cache
 * that the rest of the app reads (so existing consumers are untouched). On login we
 * hydrate the settings cache from the server, or — when the server has no UI settings yet
 * — push the local cache up once (the one-time localStorage → DB migration). Thereafter
 * writes go through `saveUi`, updating the cache immediately and persisting to the server.
 */

// The UI fields mirrored between the settings cache and the DB document.
function uiFromCache(): UiPreferences {
  const s = useSettingsStore.getState();
  return {
    compactToolCards: s.compactToolCards,
    showTimestamps: s.showTimestamps,
    showTokenCounts: s.showTokenCounts,
    wideComposer: s.wideComposer,
    sidebarWidth: s.sidebarWidth,
  };
}

function applyUiToCache(ui: UiPreferences | undefined): void {
  if (!ui) return;
  const patch: Partial<UiPreferences> = {};
  if (typeof ui.compactToolCards === 'boolean') patch.compactToolCards = ui.compactToolCards;
  if (typeof ui.showTimestamps === 'boolean') patch.showTimestamps = ui.showTimestamps;
  if (typeof ui.showTokenCounts === 'boolean') patch.showTokenCounts = ui.showTokenCounts;
  if (typeof ui.wideComposer === 'boolean') patch.wideComposer = ui.wideComposer;
  if (typeof ui.sidebarWidth === 'number') patch.sidebarWidth = ui.sidebarWidth;
  // setState merges; the persist middleware writes the cache back to localStorage.
  useSettingsStore.setState(patch);
}

interface PreferencesState {
  loaded: boolean;
  loading: boolean;
  error: string | null;
  schemaVersion: number;
  document: PreferencesDocument;
  skills: UserSkill[];
  disabledAdmin: string[];
  // Bumped by every write; load() uses it to avoid clobbering a write that landed
  // while its fetch was in flight (e.g. a toggle during the post-login hydrate).
  writeSeq: number;

  load: () => Promise<void>;
  saveUi: (patch: Partial<UiPreferences>) => Promise<void>;
  setAdminSkillEnabled: (name: string, enabled: boolean) => Promise<void>;
  createSkill: (content: string) => Promise<void>;
  updateSkill: (id: string, data: { content?: string; is_active?: boolean }) => Promise<void>;
  deleteSkill: (id: string) => Promise<void>;
  overrideSkill: (adminName: string) => Promise<void>;
  exportConfig: () => Promise<ConfigExport>;
  importConfig: (payload: ConfigExport, replaceAll: boolean) => Promise<void>;
  clearError: () => void;
}

export const usePreferencesStore = create<PreferencesState>((set, get) => ({
  loaded: false,
  loading: false,
  error: null,
  schemaVersion: 1,
  document: {},
  skills: [],
  disabledAdmin: [],
  writeSeq: 0,

  load: async () => {
    if (get().loading) return;
    const seq = get().writeSeq;
    set({ loading: true, error: null });
    try {
      const resp = await getMyPreferences();
      // A user write landed during the fetch → it is newer than this snapshot; keep it
      // and only flip loaded, rather than overwriting it with stale server state.
      if (get().writeSeq !== seq) {
        set({ loading: false, loaded: true });
        return;
      }
      const serverUi = resp.document.ui;
      let document = resp.document;
      if (serverUi && Object.keys(serverUi).length > 0) {
        // DB authoritative → hydrate the live cache from it.
        applyUiToCache(serverUi);
      } else {
        // One-time migration: server has no UI settings yet → seed it from the local cache.
        document = { ...resp.document, ui: uiFromCache() };
        await updateMyPreferences(document);
        // The migration adds a second await; re-check so a write during it isn't clobbered.
        if (get().writeSeq !== seq) {
          set({ loading: false, loaded: true });
          return;
        }
      }
      set({
        loaded: true,
        loading: false,
        schemaVersion: resp.schema_version,
        document,
        skills: resp.skills,
        disabledAdmin: document.skills?.disabled_admin ?? [],
      });
    } catch (e) {
      set({ loading: false, error: (e as Error).message });
    }
  },

  saveUi: async (patch) => {
    applyUiToCache(patch); // instant UI feedback via the cache
    const document: PreferencesDocument = { ...get().document, ui: uiFromCache() };
    set({ document, error: null, writeSeq: get().writeSeq + 1 });
    try {
      await updateMyPreferences(document);
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  setAdminSkillEnabled: async (name, enabled) => {
    const current = new Set(get().disabledAdmin);
    if (enabled) current.delete(name);
    else current.add(name);
    const disabled = [...current];
    const document: PreferencesDocument = {
      ...get().document,
      skills: { ...get().document.skills, disabled_admin: disabled },
    };
    set({ disabledAdmin: disabled, document, error: null, writeSeq: get().writeSeq + 1 });
    try {
      await updateMyPreferences(document);
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  createSkill: async (content) => {
    set({ error: null, writeSeq: get().writeSeq + 1 });
    try {
      const created = await createMySkill(content);
      set({ skills: [...get().skills, created].sort((a, b) => a.name.localeCompare(b.name)) });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  updateSkill: async (id, data) => {
    set({ error: null, writeSeq: get().writeSeq + 1 });
    try {
      const updated = await updateMySkill(id, data);
      set({
        skills: get()
          .skills.map((s) => (s.id === id ? updated : s))
          .sort((a, b) => a.name.localeCompare(b.name)),
      });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  deleteSkill: async (id) => {
    set({ error: null, writeSeq: get().writeSeq + 1 });
    try {
      await deleteMySkill(id);
      set({ skills: get().skills.filter((s) => s.id !== id) });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  overrideSkill: async (adminName) => {
    set({ error: null, writeSeq: get().writeSeq + 1 });
    try {
      const created = await overrideAdminSkill(adminName);
      set({ skills: [...get().skills, created].sort((a, b) => a.name.localeCompare(b.name)) });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  exportConfig: async () => exportMyConfig(),

  importConfig: async (payload, replaceAll) => {
    set({ error: null, writeSeq: get().writeSeq + 1 });
    try {
      const resp = await importMyConfig(payload, replaceAll);
      applyUiToCache(resp.document.ui);
      set({
        schemaVersion: resp.schema_version,
        document: resp.document,
        skills: resp.skills,
        disabledAdmin: resp.document.skills?.disabled_admin ?? [],
      });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  clearError: () => set({ error: null }),
}));
