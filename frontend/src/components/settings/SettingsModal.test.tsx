import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsModal } from './SettingsModal';
import { usePreferencesStore } from '@/store/preferences';
import { useSettingsStore } from '@/store/settings';
import * as api from '@/services/api';

describe('SettingsModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    vi.spyOn(api, 'getMyPreferences').mockResolvedValue({
      schema_version: 1,
      document: { ui: {} },
      skills: [],
    });
    vi.spyOn(api, 'updateMyPreferences').mockResolvedValue({
      schema_version: 1,
      document: {},
      skills: [],
    });
    usePreferencesStore.setState({ loaded: true, skills: [], disabledAdmin: [], document: {} });
    useSettingsStore.setState({
      compactToolCards: true,
      showTimestamps: true,
      wideComposer: false,
      sidebarWidth: 256,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the three tabs', async () => {
    render(<SettingsModal isOpen onClose={() => {}} />);
    expect(screen.getByText('Display')).toBeInTheDocument();
    expect(screen.getByText('Personal Skills')).toBeInTheDocument();
    expect(screen.getByText('Import / Export')).toBeInTheDocument();
  });

  it('toggling a display setting persists via saveUi', async () => {
    const user = userEvent.setup();
    render(<SettingsModal isOpen onClose={() => {}} />);
    await user.click(screen.getByText('Wide composer'));
    await waitFor(() => expect(api.updateMyPreferences).toHaveBeenCalled());
    expect(useSettingsStore.getState().wideComposer).toBe(true);
  });

  it('shows the export control on the Import / Export tab', async () => {
    const user = userEvent.setup();
    render(<SettingsModal isOpen onClose={() => {}} />);
    await user.click(screen.getByText('Import / Export'));
    expect(screen.getByText('Export config')).toBeInTheDocument();
  });

  it('does not render when closed', () => {
    render(<SettingsModal isOpen={false} onClose={() => {}} />);
    expect(screen.queryByText('Display')).not.toBeInTheDocument();
  });

  it('gates the body until preferences finish loading', () => {
    // load() stubbed so `loaded` stays false → no writer (toggle) is reachable, which would
    // otherwise PUT a document missing skills.disabled_admin and drop the user's choices.
    usePreferencesStore.setState({ loaded: false, load: vi.fn() });
    render(<SettingsModal isOpen onClose={() => {}} />);
    expect(screen.getByText('Display')).toBeInTheDocument(); // tabs still show
    expect(screen.queryByText('Wide composer')).not.toBeInTheDocument(); // body gated
  });
});
