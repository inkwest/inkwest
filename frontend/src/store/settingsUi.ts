import { create } from 'zustand';

// Single source of truth for the Settings tab keys: SettingsModal derives its tab list
// from it and Header validates ?settings= deep-links against it.
export const SETTINGS_TABS = ['display', 'skills', 'mcp', 'io'] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number];

export function isSettingsTab(v: string): v is SettingsTab {
  return (SETTINGS_TABS as readonly string[]).includes(v);
}

// Cross-component control of the Settings modal: lets a deep component (e.g. the composer's
// MCP panel) open Settings on a specific tab without prop-drilling through the layout.
interface SettingsUiState {
  isOpen: boolean;
  initialTab: SettingsTab;
  // Server key to pre-select on the MCP tab (a per-server cog deep-links to its tools).
  initialServer: string | null;
  open: (tab?: SettingsTab, server?: string) => void;
  close: () => void;
}

export const useSettingsUi = create<SettingsUiState>((set) => ({
  isOpen: false,
  initialTab: 'display',
  initialServer: null,
  open: (tab = 'display', server) => set({ isOpen: true, initialTab: tab, initialServer: server ?? null }),
  close: () => set({ isOpen: false }),
}));
