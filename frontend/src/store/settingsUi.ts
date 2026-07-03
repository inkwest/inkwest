import { create } from 'zustand';

// Which Settings tab to show. Kept in sync with SettingsModal's own Tab union.
export type SettingsTab = 'display' | 'skills' | 'mcp' | 'io';

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
