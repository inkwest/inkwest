import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('@/services/api', () => ({
  decideMcpAsk: vi.fn().mockResolvedValue(undefined),
  getPendingMcpAsks: vi.fn().mockResolvedValue([]),
}));

import { McpAskNotice } from './McpAskNotice';
import { useMcpStore } from '@/store/mcp';
import { decideMcpAsk, getPendingMcpAsks } from '@/services/api';

const ASK = { askId: 'a1', mcpKey: 'notion', tool: 'search' };

describe('McpAskNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMcpStore.setState({ pendingAsks: [] });
  });

  it('renders nothing when no ask is pending', () => {
    const { container } = render(<McpAskNotice sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a banner per pending ask with Allow/Deny', () => {
    useMcpStore.setState({
      pendingAsks: [ASK, { askId: 'a2', mcpKey: 'notion', tool: 'write' }],
    });
    render(<McpAskNotice sessionId="s1" />);
    expect(screen.getByText('search')).toBeInTheDocument();
    expect(screen.getByText('write')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Allow' })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: 'Deny' })).toHaveLength(2);
  });

  it('Allow posts the decision and clears the banner', async () => {
    useMcpStore.setState({ pendingAsks: [ASK] });
    render(<McpAskNotice sessionId="s1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('a1', 'allow'));
    await waitFor(() => expect(useMcpStore.getState().pendingAsks).toEqual([]));
  });

  it('Deny posts deny', async () => {
    useMcpStore.setState({ pendingAsks: [ASK] });
    render(<McpAskNotice sessionId="s1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('a1', 'deny'));
  });

  it('a failed decision (expired ask) still clears the banner', async () => {
    vi.mocked(decideMcpAsk).mockRejectedValueOnce(new Error('404'));
    useMcpStore.setState({ pendingAsks: [ASK] });
    render(<McpAskNotice sessionId="s1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    await waitFor(() => expect(useMcpStore.getState().pendingAsks).toEqual([]));
  });

  it('double-click fires a single decision', async () => {
    // Keep the POST pending so the second click lands while the first is in flight.
    let release: () => void = () => {};
    vi.mocked(decideMcpAsk).mockReturnValueOnce(
      new Promise<void>((resolve) => {
        release = resolve;
      })
    );
    useMcpStore.setState({ pendingAsks: [ASK] });
    render(<McpAskNotice sessionId="s1" />);
    const btn = screen.getByRole('button', { name: 'Allow' });
    fireEvent.click(btn);
    fireEvent.click(btn);
    release();
    await waitFor(() => expect(useMcpStore.getState().pendingAsks).toEqual([]));
    expect(decideMcpAsk).toHaveBeenCalledTimes(1);
  });

  it('rehydrates pending asks for the session on mount', async () => {
    vi.mocked(getPendingMcpAsks).mockResolvedValueOnce([
      { ask_id: 'a9', mcp_key: 'grafana', tool: 'query' },
    ]);
    render(<McpAskNotice sessionId="s1" />);
    await waitFor(() =>
      expect(useMcpStore.getState().pendingAsks).toEqual([
        { askId: 'a9', mcpKey: 'grafana', tool: 'query' },
      ])
    );
    expect(getPendingMcpAsks).toHaveBeenCalledWith('s1');
    expect(screen.getByText('grafana')).toBeInTheDocument();
  });

  it('session switch resets stale asks then rehydrates for the new session', async () => {
    useMcpStore.setState({ pendingAsks: [ASK] });
    const { rerender } = render(<McpAskNotice sessionId="s1" />);
    await waitFor(() => expect(getPendingMcpAsks).toHaveBeenCalledWith('s1'));
    vi.mocked(getPendingMcpAsks).mockResolvedValueOnce([]);
    rerender(<McpAskNotice sessionId="s2" />);
    await waitFor(() => expect(getPendingMcpAsks).toHaveBeenCalledWith('s2'));
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
  });
});
