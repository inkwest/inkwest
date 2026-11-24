import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { Header } from './Header';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { useChatStore } from '@/store/chat';
import { usePreferencesStore } from '@/store/preferences';
import { useSettingsUi } from '@/store/settingsUi';
import * as api from '@/services/api';

vi.mock('@/components/chat/ConnectionStatus', () => ({
  ConnectionStatus: () => <div data-testid="connection-status">Connected</div>,
}));

// The mcp tab's real content fetches the catalog on mount — irrelevant here.
vi.mock('@/components/settings/McpPolicySettingsTab', () => ({
  McpPolicySettingsTab: () => <div data-testid="mcp-tab" />,
}));

const mockCheckAuth = vi.fn();
const mockLogout = vi.fn();

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location-search">{loc.search}</div>;
}

function renderHeader(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Header />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe('Header', () => {
  beforeEach(() => {
    useSessionsStore.setState({ currentSession: null });
    useSettingsStore.setState({
      compactToolCards: false,
      sidebarOpen: true,
    });
    useAuthStore.setState({
      user: { id: 'u1', email: 'dev@localhost', display_name: 'Dev User', is_admin: false, roles: [], oidc_issuer: '' },
      isAuthenticated: true,
      oidcEnabled: false,
      isLoading: false,
      error: null,
      checkAuth: mockCheckAuth,
      logout: mockLogout,
    });
    // Stub fetchConfig so the mount effect never hits the network.
    useConfigStore.setState({ config: null, fetchConfig: vi.fn() });
    // Stub the prefs hydrate (mount effect) + the modal's admin-skill fetch.
    usePreferencesStore.setState({ loaded: true, load: vi.fn(), skills: [], disabledAdmin: [], document: {} });
    vi.spyOn(api, 'getSkills').mockResolvedValue([]);
    useSettingsUi.setState({ isOpen: false, initialTab: 'display', initialServer: null });
  });

  it('opens Settings on the requested tab from a ?settings= deep-link and strips the param', async () => {
    renderHeader('/?settings=mcp');
    await waitFor(() => expect(useSettingsUi.getState().isOpen).toBe(true));
    expect(useSettingsUi.getState().initialTab).toBe('mcp');
    expect(screen.getByTestId('mcp-tab')).toBeInTheDocument();
    // Param stripped (replace-navigate) so reload/back don't re-open the modal.
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/));
  });

  it('falls back to the default tab for an unknown ?settings= value', async () => {
    renderHeader('/?settings=bogus');
    await waitFor(() => expect(useSettingsUi.getState().isOpen).toBe(true));
    expect(useSettingsUi.getState().initialTab).toBe('display');
  });

  it('falls back to the default tab for ?settings=notifications when push is off (003-AC13)', async () => {
    renderHeader('/?settings=notifications');
    await waitFor(() => expect(useSettingsUi.getState().isOpen).toBe(true));
    expect(screen.queryByRole('button', { name: 'Notifications' })).not.toBeInTheDocument();
    expect(screen.getByText('Wide composer')).toBeInTheDocument();
  });

  it('renders Inkwest branding', () => {
    renderHeader();
    expect(screen.getByText('Inkwest')).toBeInTheDocument();
  });

  it('shows session title when session is active', () => {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        title: 'Debug Crash',
        cluster: { id: 'c1', name: 'prod', display_name: 'Production', is_active: true, created_at: new Date().toISOString() },
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });
    renderHeader();
    expect(screen.getByText('Debug Crash')).toBeInTheDocument();
    expect(screen.getByText('Production')).toBeInTheDocument();
  });

  it('shows token counts when session has tokens', () => {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        title: 'Test',
        total_input_tokens: 1500,
        total_output_tokens: 800,
        cluster: { id: 'c1', name: 'prod', display_name: 'Production', is_active: true, created_at: new Date().toISOString() },
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });
    renderHeader();
    // Per-symbol titles: each glyph carries its own hover (fresh input / output).
    expect(screen.getByTitle(/fresh input tokens/i)).toHaveTextContent('1.5k');
    expect(screen.getByTitle(/output tokens/i)).toHaveTextContent('800');
  });

  it('shows cache glyphs only when the session has cache tokens', () => {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        title: 'Test',
        total_input_tokens: 1500,
        total_output_tokens: 800,
        total_cache_read_tokens: 27082,
        total_cache_write_tokens: 3242,
        cluster: { id: 'c1', name: 'prod', display_name: 'Production', is_active: true, created_at: new Date().toISOString() },
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });
    renderHeader();
    expect(screen.getByTitle(/cache read: 27,082/i)).toHaveTextContent('27.1k');
    expect(screen.getByTitle(/cache write: 3,242/i)).toHaveTextContent('3.2k');
  });

  // --- context-pressure gauge -------------------------------------------------
  function primeContextSession(contextWindow: number | null) {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        title: 'Test',
        model: 'claude-sonnet-5',
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } as never,
    });
    useConfigStore.setState({
      config: {
        model: 'claude-sonnet-5',
        default_engine: 'claude-cli',
        engines: ['claude-cli'],
        attachments_enabled: false,
        available_models: [
          {
            id: 'claude-sonnet-5',
            label: 'Sonnet 5',
            legacy: false,
            engines: ['claude-cli'],
            input_modalities: [],
            context_window: contextWindow,
          },
        ],
        push_public_key: null,
      },
      fetchConfig: vi.fn(),
    });
    useChatStore.setState({
      messages: [
        {
          id: 'm1',
          session_id: 's1',
          role: 'assistant',
          content: 'done',
          input_tokens: 5000,
          cache_read_tokens: 120000,
          cache_write_tokens: 3000,
          created_at: new Date().toISOString(),
        },
      ] as never,
    });
  }

  it('shows the context gauge when the model has a known window', () => {
    primeContextSession(200_000);
    renderHeader();
    // 5000 fresh + 120000 cache-read + 3000 cache-write = 128000 of 200000 = 64%
    expect(screen.getByTitle(/128,000 of 200,000 tokens \(64%\)/i)).toHaveTextContent('64%');
  });

  it('hides the context gauge when the window is unknown', () => {
    primeContextSession(null);
    renderHeader();
    expect(screen.queryByTitle(/of .* tokens/i)).not.toBeInTheDocument();
  });

  it('shows connection status when session is active', () => {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        cluster: { id: 'c1', name: 'prod', display_name: 'Production', is_active: true, created_at: new Date().toISOString() },
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });
    renderHeader();
    expect(screen.getByTestId('connection-status')).toBeInTheDocument();
  });

  it('renders no default-model badge (selection moved to the chat toolbar picker)', () => {
    useConfigStore.setState({
      config: {
        model: 'claude-sonnet-4-20250514',
        default_engine: 'claude-cli',
        engines: ['claude-cli'],
        available_models: [
          {
            id: 'claude-sonnet-4-20250514',
            label: 'Sonnet 4.0',
            legacy: true,
            engines: ['claude-cli'],
            input_modalities: [],
          },
        ],
        attachments_enabled: false,
        push_public_key: null,
      },
    });
    renderHeader();
    // The old "Provider: … · Default model: …" badge was removed — a bare model name
    // with no engine is ambiguous now a model can run on more than one engine.
    expect(screen.queryByTitle(/Provider:/)).not.toBeInTheDocument();
  });

  it('opens profile menu on click', async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    // Display toggles moved into the Settings modal; the menu now offers a Settings entry.
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Dev User')).toBeInTheDocument();
    expect(screen.getByText('dev@localhost')).toBeInTheDocument();
  });

  it('opens the Settings modal from the menu', async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    await user.click(screen.getByText('Settings'));
    // The modal is open when its tabs are visible.
    expect(await screen.findByText('Display')).toBeInTheDocument();
    expect(screen.getByText('Import / Export')).toBeInTheDocument();
  });

  it('shows admin link for admin users', async () => {
    useAuthStore.setState({
      user: { id: 'u1', email: 'admin@localhost', display_name: 'Admin', is_admin: true, roles: ['admin'], oidc_issuer: '' },
    });
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    expect(screen.getByText('Admin Panel')).toBeInTheDocument();
  });

  it('hides admin link for non-admin users', async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    expect(screen.queryByText('Admin Panel')).not.toBeInTheDocument();
  });

  it('hides session context inside the admin panel', () => {
    useSessionsStore.setState({
      currentSession: {
        id: 's1',
        cluster_id: 'c1',
        title: 'Debug Crash',
        total_input_tokens: 1500,
        total_output_tokens: 800,
        cluster: { id: 'c1', name: 'prod', display_name: 'Production', is_active: true, created_at: new Date().toISOString() },
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });
    renderHeader('/admin/clusters');

    expect(screen.queryByText('Debug Crash')).not.toBeInTheDocument();
    expect(screen.queryByText('Production')).not.toBeInTheDocument();
    expect(screen.queryByTitle(/input.*output/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('connection-status')).not.toBeInTheDocument();
  });

  it('shows back-to-chat link instead of admin link inside the admin panel', async () => {
    useAuthStore.setState({
      user: { id: 'u1', email: 'admin@localhost', display_name: 'Admin', is_admin: true, roles: ['admin'], oidc_issuer: '' },
    });
    const user = userEvent.setup();
    renderHeader('/admin/system-prompts');

    await user.click(screen.getByTitle('Profile & Settings'));
    expect(screen.getByText('Back to Chat')).toBeInTheDocument();
    expect(screen.queryByText('Admin Panel')).not.toBeInTheDocument();
  });

  it('shows logout when OIDC enabled', async () => {
    useAuthStore.setState({ oidcEnabled: true });
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    expect(screen.getByText('Logout')).toBeInTheDocument();
  });

  it('hides logout when OIDC disabled', async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(screen.getByTitle('Profile & Settings'));
    expect(screen.queryByText('Logout')).not.toBeInTheDocument();
  });
});
