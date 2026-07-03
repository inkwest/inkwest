import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { McpPanel } from './McpPanel';
import { resetMcpCatalogCache, primeMcpCatalog } from './mcpCatalogCache';
import { useMcpStore } from '@/store/mcp';
import { useSettingsUi } from '@/store/settingsUi';
import type { McpServerEntry } from '@/services/api';

vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn(),
  getSessionMcp: vi.fn(),
  setSessionMcp: vi.fn(),
  startMcpAuth: vi.fn(),
  disconnectMcpAuth: vi.fn(),
  // The modal's right slide-out renders McpToolPolicyEditor, which calls these.
  getMcpServerTools: vi.fn().mockResolvedValue({ key: 'k', tools: [], fetched_at: null }),
  getMyMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  getSessionMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  setMyMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  setSessionMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
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
    useMcpStore.setState({ catalogVersion: 0 }); // live-reload counter must start cold
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

    const connect = await screen.findByRole('button', { name: /click to connect/i });
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
    const connect = await screen.findByRole('button', { name: /click to connect/i });
    fireEvent.click(connect);
    fireEvent.click(connect); // double-click: guard must drop this one
    expect(openSpy).toHaveBeenCalledTimes(1);
    openSpy.mockRestore();
  });

  it('shows a spinning "connecting…" status while the connect poll is in flight', async () => {
    // closed:false keeps the poll alive so the transient connecting state persists for the assert.
    const popup = { closed: false, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    mockStartAuth.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize' });
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    fireEvent.click(await screen.findByRole('button', { name: /click to connect/i }));
    // The status icon flips to the connecting affordance (still clickable to retry).
    expect(await screen.findByRole('button', { name: /connecting/i })).toBeInTheDocument();
    openSpy.mockRestore();
  });

  it("slides out a server's per-conversation tools when its row is selected", async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix']));
    mockGetSession.mockResolvedValue({ enabled: [] });
    render(<McpPanel sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    fireEvent.click(screen.getByText('coralogix')); // select the row → tools panel slides out
    expect(await screen.findByText(/Tools — coralogix/i)).toBeInTheDocument();
  });

  it('per-server cog opens global tool settings on the MCP tab, preselecting that server', async () => {
    useSettingsUi.setState({ isOpen: false, initialTab: 'display', initialServer: null });
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'connected' });
    fireEvent.click(await screen.findByRole('button', { name: 'Tool permissions for notion' }));
    expect(useSettingsUi.getState().isOpen).toBe(true);
    expect(useSettingsUi.getState().initialTab).toBe('mcp');
    expect(useSettingsUi.getState().initialServer).toBe('notion');
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
    fireEvent.click(await screen.findByRole('button', { name: /click to disconnect/i }));
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

  it('shows a needs-auth status icon that reconnects for an expired grant', async () => {
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'needs_auth' });
    // Status + action fused into one icon: its accessible name carries state + affordance.
    expect(await screen.findByRole('button', { name: /needs auth — click to reconnect/i })).toBeInTheDocument();
  });

  it('shows a connected status icon and disconnects on click', async () => {
    mockDisconnect.mockResolvedValue(cat([], []));
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'connected' });
    fireEvent.click(await screen.findByRole('button', { name: /connected — click to disconnect/i }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledWith('notion'));
  });

  it('surfaces the backend reason when starting authorization fails', async () => {
    const popup = { closed: false, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    // The backend 502/500 detail arrives as an Error message (ApiError extends Error).
    mockStartAuth.mockRejectedValue(new Error('no usable authorization-server metadata'));

    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' });
    fireEvent.click(await screen.findByRole('button', { name: /click to connect/i }));

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
    const connect = await screen.findByRole('button', { name: /click to connect/i });
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

  it('greys out an unavailable oauth server (sandbox disabled) with no connect action', async () => {
    await openModalWithOauth({ key: 'notion', kind: 'oauth2', auth_status: 'unavailable' });
    // The status icon states "sandbox required" and is disabled — no connect affordance.
    const status = await screen.findByRole('button', { name: /unavailable — sandbox required/i });
    expect(status).toBeDisabled();
    expect(screen.queryByRole('button', { name: /click to connect/i })).not.toBeInTheDocument();
  });

  // ---- stale-while-revalidate + live reload ------------------------------ //
  it('renders the peek seed synchronously (no flicker) yet still force-revalidates on mount', async () => {
    // Warm the process-wide cache: a remount must render the known catalog at once...
    primeMcpCatalog(cat(['coralogix']));
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);
    // ...synchronously — before any fetch resolves — the seeded key is already offered.
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
    // ...and it still force-fetches (SWR), so a since-changed catalog converges.
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
  });

  it('reloads the catalog when the store catalogVersion bumps (live WS invalidate)', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" />);
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalledTimes(1));

    // A WS `mcp_catalog_changed` resets the cache then bumps the version; the effect re-loads.
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    act(() => {
      resetMcpCatalogCache();
      useMcpStore.getState().bumpCatalogVersion();
    });
    // Live-reload is jittered (0..1s) to smear the broadcast herd, so allow past the max delay.
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalledTimes(2), { timeout: 3000 });
    expect(await screen.findByText('0/2 enabled', {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it('a live catalog change does not reset an open modal unsaved draft', async () => {
    // Regression: a global `mcp_catalog_changed` bump must refresh the catalog set WITHOUT
    // refetching the session selection — else it would clobber the user's in-progress toggles.
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    const gitlab = await screen.findByRole('checkbox', { name: 'gitlab' });
    fireEvent.click(gitlab); // unsaved draft toggle
    expect(gitlab).toBeChecked();

    // An admin adds a server → WS bump. The new server must appear, the draft toggle must stay.
    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab', 'newsrv']));
    act(() => {
      resetMcpCatalogCache();
      useMcpStore.getState().bumpCatalogVersion();
    });

    // Jittered live-reload (0..1s) — wait past the max delay for the new server to appear.
    expect(
      await screen.findByRole('checkbox', { name: 'newsrv' }, { timeout: 3000 })
    ).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'gitlab' })).toBeChecked();
    // The selection endpoint is fetched once (mount) — never again on the catalog bump.
    expect(mockGetSession).toHaveBeenCalledTimes(1);
  });

  it('a stale in-flight catalog fetch never clobbers a newer one (seq guard, out-of-order)', async () => {
    // autoOpen fires both the mount load and the open revalidate concurrently. Resolve the
    // later-sequenced (open) fetch FIRST, the earlier (mount) fetch LAST: the guard must let
    // the newer result stand rather than the stale mount response overwriting it.
    let resolveMount!: () => void;
    let resolveOpen!: () => void;
    mockGetCatalog
      .mockReturnValueOnce(new Promise((r) => { resolveMount = () => r(cat(['stale'])); }))
      .mockReturnValueOnce(new Promise((r) => { resolveOpen = () => r(cat(['fresh'])); }));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" autoOpen />);
    await act(async () => { resolveOpen(); });   // newer (seq 2) lands first
    await act(async () => { resolveMount(); });   // stale (seq 1) lands last — must be dropped

    expect(await screen.findByRole('checkbox', { name: 'fresh' })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: 'stale' })).not.toBeInTheDocument();
  });

  it('revalidates the catalog when the modal opens (long-mounted, missed the WS bump)', async () => {
    mockGetCatalog.mockResolvedValue(cat(['coralogix']));
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" />);
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalledTimes(1));

    mockGetCatalog.mockResolvedValue(cat(['coralogix', 'gitlab']));
    fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalledTimes(2));
  });
});
