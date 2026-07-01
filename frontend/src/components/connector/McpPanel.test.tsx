import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { McpPanel } from './McpPanel';
import { resetMcpCatalogCache } from './mcpCatalogCache';
import type { McpServerEntry } from '@/services/api';

vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn(),
  getSessionMcp: vi.fn(),
  setSessionMcp: vi.fn(),
  startMcpAuth: vi.fn(),
  disconnectMcpAuth: vi.fn(),
}));

import {
  getMcpCatalog,
  getSessionMcp,
  setSessionMcp,
  startMcpAuth,
  disconnectMcpAuth,
} from '@/services/api';

const mockGetCatalog = vi.mocked(getMcpCatalog);
const mockGetSession = vi.mocked(getSessionMcp);
const mockSetSession = vi.mocked(setSessionMcp);
const mockStartAuth = vi.mocked(startMcpAuth);
const mockDisconnect = vi.mocked(disconnectMcpAuth);

// Build the catalog response: static `keys` plus the OAuth-aware `servers` superset.
function cat(keys: string[] = [], oauth: McpServerEntry[] = []) {
  return {
    keys,
    servers: [
      ...keys.map((k) => ({ key: k, kind: 'static' as const, auth_status: 'static' as const })),
      ...oauth,
    ],
  };
}

describe('McpPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetMcpCatalogCache(); // process-wide cache must not leak between tests
  });

  it('renders nothing when the catalog is empty', async () => {
    mockGetCatalog.mockResolvedValue(cat([]));
    mockGetSession.mockResolvedValue({ enabled: [] });

    const { container } = render(<McpPanel sessionId="s1" />);
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('can stay visible with an empty catalog without offering configuration', async () => {
    mockGetCatalog.mockResolvedValue(cat([]));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
    expect(screen.getByText('MCP servers')).toBeInTheDocument();
    expect(screen.getByText('none')).toBeInTheDocument();
    expect(screen.getByText('Permissions')).toBeInTheDocument();
    expect(screen.getByText('No access granted')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Configure' })).not.toBeInTheDocument();
    expect(screen.queryByText('No MCP servers are configured.')).not.toBeInTheDocument();
  });

  it('keeps configuration available and shows a gray zero count when no catalog server is selected', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
    expect(screen.getByText('MCP servers')).toBeInTheDocument();
    // findByText: the badge renders only after the catalog promise resolves
    const badge = await screen.findByText('0/1 enabled');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('gray');
    expect(screen.getByText('No access granted')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
  });

  it('shows selected count and only selected servers in the row overlay', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab', 'prometheus']));
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    expect(await screen.findByText('1/3 enabled')).toBeInTheDocument();
    expect(screen.getByText('coralogix')).toBeInTheDocument();
    expect(screen.queryByText('gitlab')).not.toBeInTheDocument();
    expect(screen.queryByText('prometheus')).not.toBeInTheDocument();
  });

  it('shows selected/total and green styling when servers are enabled', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });

    render(<McpPanel sessionId="s1" />);

    const badge = await screen.findByText('1/2 enabled');
    expect(badge.className).toContain('green');
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
    expect(screen.getByText('Permissions')).toBeInTheDocument();
    expect(screen.getByText('coralogix')).toBeInTheDocument();
    expect(screen.queryByText('gitlab')).not.toBeInTheDocument();
  });

  it('shows 0/total and no green styling when nothing is enabled', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" />);

    const badge = await screen.findByText('0/2 enabled');
    expect(badge.className).not.toContain('green');
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
    expect(screen.getByText('No access granted')).toBeInTheDocument();
  });

  it('persists selected servers only when OK is clicked and then closes the modal', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });
    mockSetSession.mockResolvedValue({ enabled: ['coralogix', 'gitlab'] });

    render(<McpPanel sessionId="s1" />);

    const btn = await screen.findByRole('button', { name: 'Configure' });
    fireEvent.click(btn);

    // One checkbox per catalog key; the enabled one is checked.
    const coralogix = screen.getByRole('checkbox', { name: 'coralogix' });
    const gitlab = screen.getByRole('checkbox', { name: 'gitlab' });
    expect(coralogix).toBeChecked();
    expect(gitlab).not.toBeChecked();

    fireEvent.click(gitlab);
    expect(mockSetSession).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.getByText('2/2 selected')).toBeInTheDocument()
    );

    expect(screen.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() =>
      expect(mockSetSession).toHaveBeenCalledWith('s1', ['coralogix', 'gitlab'])
    );
    expect(screen.queryByText('Choose which MCP servers this conversation can use.')).not.toBeInTheDocument();
  });

  // ---- OAuth servers ----------------------------------------------------- //
  async function openModalWithOauth(server: McpServerEntry) {
    mockGetCatalog.mockResolvedValue(cat([], [server]));
    mockGetSession.mockResolvedValue({ enabled: [] });
    render(<McpPanel sessionId="s1" />);
    const btn = await screen.findByRole('button', { name: 'Configure' });
    fireEvent.click(btn);
  }

  it('offers Connect for a not_connected oauth server and opens the popup synchronously', async () => {
    const popup = { closed: true, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    mockStartAuth.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize?x=1' });

    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });

    const connect = await screen.findByRole('button', { name: 'Connect' });
    fireEvent.click(connect);
    // Popup must open synchronously in the click handler (before the await) — else blocked.
    expect(openSpy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(mockStartAuth).toHaveBeenCalledWith('notion'));
    await waitFor(() => expect(popup.location.href).toBe('https://as.example.com/authorize?x=1'));
    openSpy.mockRestore();
  });

  it('ignores a second Connect click while a poll is already in flight', async () => {
    // closed:false keeps the poll alive so the in-flight guard stays armed for the 2nd click.
    const popup = { closed: false, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    mockStartAuth.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize' });
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    const connect = await screen.findByRole('button', { name: 'Connect' });
    fireEvent.click(connect);
    fireEvent.click(connect); // double-click: guard must drop this one
    expect(openSpy).toHaveBeenCalledTimes(1);
    openSpy.mockRestore();
  });

  it('drops the key from the session selection BEFORE revoking upstream on Disconnect', async () => {
    mockGetCatalog.mockResolvedValue(
      cat([], [{ key: 'notion', kind: 'oauth2', auth_status: 'connected' }])
    );
    mockGetSession.mockResolvedValue({ enabled: ['notion'] });
    mockSetSession.mockResolvedValue({ enabled: [] });
    mockDisconnect.mockResolvedValue(
      cat([], [{ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' }])
    );
    render(<McpPanel sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledWith('notion'));
    // setSessionMcp must have run first, so a failed revoke can't leave it enabled-but-revoked.
    expect(mockSetSession.mock.invocationCallOrder[0]).toBeLessThan(
      mockDisconnect.mock.invocationCallOrder[0]
    );
  });

  it('enables a connected oauth server into the conversation selection on OK', async () => {
    mockSetSession.mockResolvedValue({ enabled: ['notion'] });
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'connected' });
    const checkbox = await screen.findByRole('checkbox'); // only the oauth row has one here
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));
    await waitFor(() => expect(mockSetSession).toHaveBeenCalledWith('s1', ['notion']));
  });

  it('disables the enable checkbox until the oauth server is connected', async () => {
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    expect(await screen.findByRole('checkbox')).toBeDisabled();
  });

  it('shows a needs-auth badge and a Reconnect button for an expired grant', async () => {
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'needs_auth' });
    expect(await screen.findByText('needs auth')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
  });

  it('shows a connected badge and Disconnect, and disconnects on click', async () => {
    mockDisconnect.mockResolvedValue(cat([], []));
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'connected' });
    expect(await screen.findByText('connected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledWith('notion'));
  });

  it('surfaces the backend reason when starting authorization fails', async () => {
    const popup = { closed: false, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    // The backend 502/500 detail arrives as an Error message (ApiError extends Error).
    mockStartAuth.mockRejectedValue(new Error('no usable authorization-server metadata'));

    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    expect(
      await screen.findByText(
        'Failed to start MCP authorization: no usable authorization-server metadata',
      ),
    ).toBeInTheDocument();
    expect(popup.close).toHaveBeenCalled(); // blank popup is closed, not left hanging
    openSpy.mockRestore();
  });

  it('clears a prior connect error the instant the user retries', async () => {
    const popup = { closed: false, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    mockStartAuth.mockRejectedValueOnce(new Error('boom'));

    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    const connect = await screen.findByRole('button', { name: 'Connect' });
    fireEvent.click(connect);
    expect(await screen.findByText('Failed to start MCP authorization: boom')).toBeInTheDocument();

    // Retry: setError(null) runs BEFORE the in-flight guard, so the stale error clears at once —
    // no dependency on the 2s poll freeing the guard (which is why this is deterministic).
    fireEvent.click(connect);
    await waitFor(() =>
      expect(
        screen.queryByText('Failed to start MCP authorization: boom'),
      ).not.toBeInTheDocument(),
    );
    openSpy.mockRestore();
  });

  it('greys out an unavailable oauth server (sandbox disabled) with no Connect', async () => {
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'unavailable' });
    expect(await screen.findByText('sandbox required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument();
  });
});
