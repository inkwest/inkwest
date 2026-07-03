import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { McpPolicySettingsTab } from './McpPolicySettingsTab';
import { resetMcpCatalogCache } from '@/components/connector/mcpCatalogCache';
import { useSettingsUi } from '@/store/settingsUi';
import type { McpServerEntry } from '@/services/api';

vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn(),
  startMcpAuth: vi.fn(),
}));

// The right pane's real editor fetches tools/policy on mount — stub it so these tests focus on
// the connect gating, not the editor internals (covered by McpToolPolicyEditor.test).
vi.mock('@/components/connector/McpToolPolicyEditor', () => ({
  McpToolPolicyEditor: ({ serverKey }: { serverKey: string }) => (
    <div data-testid="tool-editor">editor:{serverKey}</div>
  ),
}));

import { getMcpCatalog, startMcpAuth } from '@/services/api';

const mockGetCatalog = vi.mocked(getMcpCatalog);
const mockStartAuth = vi.mocked(startMcpAuth);

function cat(servers: McpServerEntry[]) {
  return { keys: servers.filter((s) => s.kind === 'static').map((s) => s.key), servers };
}

describe('McpPolicySettingsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetMcpCatalogCache(); // process-wide catalog cache must not leak between tests
    useSettingsUi.setState({ initialServer: null });
  });

  it('shows the tool editor for a connected/static server', async () => {
    mockGetCatalog.mockResolvedValue(cat([{ key: 'coralogix', kind: 'static', auth_status: 'static' }]));
    render(<McpPolicySettingsTab />);
    expect(await screen.findByTestId('tool-editor')).toHaveTextContent('editor:coralogix');
    expect(screen.queryByRole('button', { name: /connect/i })).not.toBeInTheDocument();
  });

  it('offers Connect (not the tool editor) for a not_connected oauth server', async () => {
    mockGetCatalog.mockResolvedValue(cat([{ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' }]));
    render(<McpPolicySettingsTab />);
    expect(await screen.findByRole('button', { name: 'Connect' })).toBeInTheDocument();
    expect(screen.queryByTestId('tool-editor')).not.toBeInTheDocument();
  });

  it('offers Connect for an expired grant (needs_auth)', async () => {
    mockGetCatalog.mockResolvedValue(cat([{ key: 'notion', kind: 'oauth2', auth_status: 'needs_auth' }]));
    render(<McpPolicySettingsTab />);
    expect(await screen.findByRole('button', { name: 'Connect' })).toBeInTheDocument();
  });

  it('opens the OAuth popup and starts auth when Connect is clicked', async () => {
    const popup = { closed: true, location: { href: '' }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    mockGetCatalog.mockResolvedValue(cat([{ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' }]));
    mockStartAuth.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize' });

    render(<McpPolicySettingsTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    expect(openSpy).toHaveBeenCalledTimes(1); // popup opened synchronously in the click handler
    await waitFor(() => expect(mockStartAuth).toHaveBeenCalledWith('notion'));
    openSpy.mockRestore();
  });

  it('swaps the prompt for the tool editor once the poll observes a connected grant', async () => {
    vi.useFakeTimers();
    try {
      const popup = { closed: false, location: { href: '' }, close: vi.fn() };
      vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
      mockGetCatalog.mockResolvedValue(cat([{ key: 'notion', kind: 'oauth2', auth_status: 'not_connected' }]));
      mockStartAuth.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize' });

      render(<McpPolicySettingsTab />);
      // findBy* drives fake timers itself; flush the mount fetch with a manual tick instead.
      await vi.advanceTimersByTimeAsync(0);
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

      // The grant lands upstream — the next poll fetch reports it connected.
      mockGetCatalog.mockResolvedValue(cat([{ key: 'notion', kind: 'oauth2', auth_status: 'connected' }]));
      await vi.advanceTimersByTimeAsync(2000); // one poll interval

      expect(screen.getByTestId('tool-editor')).toHaveTextContent('editor:notion');
      expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
