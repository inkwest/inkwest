import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ToolCallCard } from './ToolCallCard';
import { McpAskNotice } from './McpAskNotice';
import { useMcpStore } from '@/store/mcp';

vi.mock('@/services/api', () => ({
  decideMcpAsk: vi.fn().mockResolvedValue(undefined),
  getPendingMcpAsks: vi.fn().mockResolvedValue([]),
  setSessionMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  setMyMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  // Existing entries prove the fetch→merge→PUT keeps the server's other verdicts (the
  // endpoint is a full-state replace).
  getSessionMcpPolicy: vi.fn().mockResolvedValue({
    policies: [{ server_key: 'mcp-auth-kc', tool: 'other-tool', verdict: 'allow' }],
  }),
  getMyMcpPolicy: vi.fn().mockResolvedValue({
    policies: [
      { server_key: 'mcp-auth-kc', tool: '*', verdict: 'ask' },
      { server_key: 'unrelated', tool: 'x', verdict: 'deny' },
    ],
  }),
}));

import { decideMcpAsk, setMyMcpPolicy, setSessionMcpPolicy } from '@/services/api';
import { useSessionsStore } from '@/store/sessions';

const ASK = { askId: 'ask-1', mcpKey: 'mcp-auth-kc', tool: 'list-users' };

describe('ToolCallCard runtime ask gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMcpStore.setState({ pendingAsks: [], claimedAsks: {} });
  });

  it('renders Allow/Deny inside the loading card matching the pending ask', () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Deny' })).toBeInTheDocument();
  });

  it('matches the single-underscore tool-name variant too', () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp_mcp-auth-kc_list-users" isLoading />);
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
  });

  it('keeps the bar while the call has no result, even if loading flips off', () => {
    useMcpStore.getState().addAsk(ASK);
    const { rerender } = render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    rerender(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading={false} />);
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
    rerender(<ToolCallCard name="mcp__mcp-auth-kc__list-users" result="done" />);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
  });

  it('does not render the bar when the tool name differs or the call completed', () => {
    useMcpStore.getState().addAsk(ASK);
    const { rerender } = render(<ToolCallCard name="mcp__other__tool" isLoading />);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
    rerender(<ToolCallCard name="mcp__mcp-auth-kc__list-users" result="42" />);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
  });

  it('claims the ask so the fallback banner hides it, and releases on unmount', async () => {
    useMcpStore.getState().addAsk(ASK);
    // Regression: card + banner mounted together used to claim/release-loop into
    // "Maximum update depth exceeded" (white page). Both must settle in one pass.
    const { unmount } = render(
      <>
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />
        <McpAskNotice sessionId="s1" />
      </>,
    );
    await waitFor(() => expect(useMcpStore.getState().claimedAsks['ask-1']).toBe(true));
    // In-bubble bar shows; the banner (which mentions the server via "wants to call") hides.
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
    expect(screen.queryByText(/wants to call/)).not.toBeInTheDocument();
    unmount();
    expect(useMcpStore.getState().claimedAsks['ask-1']).toBeUndefined();
  });

  it('primary click decides once (no policy write) and clears the ask', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'allow'));
    expect(setSessionMcpPolicy).not.toHaveBeenCalled();
    expect(setMyMcpPolicy).not.toHaveBeenCalled();
    expect(useMcpStore.getState().pendingAsks).toHaveLength(0);
  });

  it('menu scope "for this conversation" persists the session policy then resolves', async () => {
    useSessionsStore.setState({ currentSession: { id: 'sess-1' } as never });
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Deny options' }));
    fireEvent.click(screen.getByRole('button', { name: 'Deny for this conversation' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'deny'));
    expect(setSessionMcpPolicy).toHaveBeenCalledWith('sess-1', 'mcp-auth-kc', [
      { tool: 'other-tool', verdict: 'allow' },
      { tool: 'list-users', verdict: 'deny' },
    ]);
    useSessionsStore.setState({ currentSession: null } as never);
  });

  it('menu scope "always" persists the global policy then resolves', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow options' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow always (all conversations)' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'allow'));
    expect(setMyMcpPolicy).toHaveBeenCalledWith('mcp-auth-kc', [
      { tool: '*', verdict: 'ask' },
      { tool: 'list-users', verdict: 'allow' },
    ]);
  });
});
