import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('@/lib/mcpConnect', () => ({
  openMcpAuthPopup: vi.fn(() => ({ closed: true })),
}));
vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn().mockResolvedValue({ keys: [], servers: [] }),
}));

import { McpInlineNotice } from './McpInlineNotice';
import { useMcpStore } from '@/store/mcp';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { getMcpCatalog } from '@/services/api';
import { peekMcpCatalog, resetMcpCatalogCache } from '@/components/connector/mcpCatalogCache';

describe('McpInlineNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetMcpCatalogCache();
    useMcpStore.setState({ authRequired: [] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when no MCP server needs auth', () => {
    const { container } = render(<McpInlineNotice />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a reconnect banner for a flagged server', () => {
    useMcpStore.getState().flagAuthRequired('notion');
    render(<McpInlineNotice />);
    expect(screen.getByText(/needs reconnecting/i)).toBeInTheDocument();
    expect(screen.getByText('notion')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
  });

  it('Reconnect opens the OAuth popup for that key', async () => {
    useMcpStore.getState().flagAuthRequired('notion');
    render(<McpInlineNotice />);
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    await waitFor(() => expect(openMcpAuthPopup).toHaveBeenCalledWith('notion'));
  });

  it('ignores a second Reconnect click while a poll is already in flight', async () => {
    useMcpStore.getState().flagAuthRequired('notion');
    render(<McpInlineNotice />);
    const btn = screen.getByRole('button', { name: 'Reconnect' });
    fireEvent.click(btn);
    fireEvent.click(btn); // double-click: the in-flight guard must drop this one
    await waitFor(() => expect(openMcpAuthPopup).toHaveBeenCalledTimes(1));
  });

  it('a reconnect poll tick primes the shared MCP catalog cache', async () => {
    vi.useFakeTimers();
    const fresh = {
      keys: ['notion'],
      servers: [{ key: 'notion', kind: 'oauth2' as const, auth_status: 'needs_auth' as const }],
    };
    vi.mocked(getMcpCatalog).mockResolvedValue(fresh);
    useMcpStore.getState().flagAuthRequired('notion');
    render(<McpInlineNotice />);
    expect(peekMcpCatalog()).toBeNull(); // cold before the poll
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    await vi.advanceTimersByTimeAsync(2000); // one poll tick → force-fetch + prime
    // The live poll routed through loadMcpCatalog(true), so the shared cache is now warm:
    // a McpPanel remount would render the up-to-date catalog without its own refetch.
    expect(peekMcpCatalog()).toEqual(fresh);
  });
});
