import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { Header } from './Header';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';

vi.mock('@/components/chat/ConnectionStatus', () => ({
  ConnectionStatus: () => <div data-testid="connection-status">Connected</div>,
}));

const mockCheckAuth = vi.fn();
const mockLogout = vi.fn();

function renderHeader(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Header />
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
    const tokenEl = screen.getByTitle(/input.*output/i);
    expect(tokenEl).toBeInTheDocument();
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
        provider: 'anthropic',
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
    expect(screen.getByText('Compact tool cards')).toBeInTheDocument();
    expect(screen.getByText('Dev User')).toBeInTheDocument();
    expect(screen.getByText('dev@localhost')).toBeInTheDocument();
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
