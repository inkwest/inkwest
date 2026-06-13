import { describe, it, expect, vi, beforeEach } from 'vitest';
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

describe('McpInlineNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMcpStore.setState({ authRequired: [] });
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
});
