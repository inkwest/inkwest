import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { McpPanel } from './McpPanel';

vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn(),
  getSessionMcp: vi.fn(),
  setSessionMcp: vi.fn(),
}));

import { getMcpCatalog, getSessionMcp, setSessionMcp } from '@/services/api';

const mockGetCatalog = vi.mocked(getMcpCatalog);
const mockGetSession = vi.mocked(getSessionMcp);
const mockSetSession = vi.mocked(setSessionMcp);

describe('McpPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing when the catalog is empty', async () => {
    mockGetCatalog.mockResolvedValue({ keys: [] });
    mockGetSession.mockResolvedValue({ enabled: [] });

    const { container } = render(<McpPanel sessionId="s1" />);
    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('can stay visible with an empty catalog without offering configuration', async () => {
    mockGetCatalog.mockResolvedValue({ keys: [] });
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
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix'] });
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    await waitFor(() => expect(mockGetCatalog).toHaveBeenCalled());
    expect(screen.getByText('MCP servers')).toBeInTheDocument();
    const badge = screen.getByText('0/1 enabled');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('gray');
    expect(screen.getByText('No access granted')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
  });

  it('shows selected count and only selected servers in the row overlay', async () => {
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix', 'gitlab', 'prometheus'] });
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    expect(await screen.findByText('1/3 enabled')).toBeInTheDocument();
    expect(screen.getByText('coralogix')).toBeInTheDocument();
    expect(screen.queryByText('gitlab')).not.toBeInTheDocument();
    expect(screen.queryByText('prometheus')).not.toBeInTheDocument();
  });

  it('adds time left to the selected count when the MCP selection has an expiry', async () => {
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix', 'gitlab', 'prometheus'] });
    mockGetSession.mockResolvedValue({
      enabled: ['coralogix'],
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    });

    render(<McpPanel sessionId="s1" variant="row" showWhenEmpty />);

    expect(await screen.findByText(/1\/3 enabled · .* left/)).toBeInTheDocument();
  });

  it('shows selected/total and green styling when servers are enabled', async () => {
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix', 'gitlab'] });
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });

    render(<McpPanel sessionId="s1" />);

    const btn = await screen.findByRole('button', { name: /MCP 1\/2/ });
    expect(btn.className).toContain('green');
  });

  it('shows 0/total and no green styling when nothing is enabled', async () => {
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix', 'gitlab'] });
    mockGetSession.mockResolvedValue({ enabled: [] });

    render(<McpPanel sessionId="s1" />);

    const btn = await screen.findByRole('button', { name: /MCP 0\/2/ });
    expect(btn.className).not.toContain('green');
  });

  it('lists servers with checkboxes in the modal and persists a toggle', async () => {
    mockGetCatalog.mockResolvedValue({ keys: ['coralogix', 'gitlab'] });
    mockGetSession.mockResolvedValue({ enabled: ['coralogix'] });
    mockSetSession.mockResolvedValue({ enabled: ['coralogix', 'gitlab'] });

    render(<McpPanel sessionId="s1" />);

    const btn = await screen.findByRole('button', { name: /MCP/ });
    fireEvent.click(btn);

    // One checkbox per catalog key; the enabled one is checked.
    const coralogix = screen.getByRole('checkbox', { name: 'coralogix' });
    const gitlab = screen.getByRole('checkbox', { name: 'gitlab' });
    expect(coralogix).toBeChecked();
    expect(gitlab).not.toBeChecked();

    fireEvent.click(gitlab);

    await waitFor(() =>
      expect(mockSetSession).toHaveBeenCalledWith('s1', ['coralogix', 'gitlab'])
    );
  });
});
