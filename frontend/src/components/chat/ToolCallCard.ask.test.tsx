import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ToolCallCard } from './ToolCallCard';
import { McpAskNotice } from './McpAskNotice';
import { useMcpStore } from '@/store/mcp';
import { resetPolicySaveChainForTests } from '@/store/mcpPolicy';

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

import { decideMcpAsk, getMyMcpPolicy, setMyMcpPolicy, setSessionMcpPolicy } from '@/services/api';

const ASK = { askId: 'ask-1', mcpKey: 'mcp-auth-kc', tool: 'list-users', sessionId: 'sess-1' };

describe('ToolCallCard runtime ask gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetPolicySaveChainForTests();
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

  it('hides the bar when the ask is resolved externally (another tab decided)', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
    // The mcp_tool_ask_resolved WS frame lands as a store removeAsk.
    act(() => useMcpStore.getState().removeAsk('ask-1'));
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
    // In-bubble bar shows; the composer-anchored fallback (McpAskNotice) hides.
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument();
    expect(screen.queryByTestId('mcp-ask-fallback')).not.toBeInTheDocument();
    unmount();
    expect(useMcpStore.getState().claimedAsks['ask-1']).toBeUndefined();
  });

  it('sibling cards for the same tool arbitrate: exactly one decision bar', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(
      <>
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />
      </>,
    );
    await waitFor(() => expect(useMcpStore.getState().claimedAsks['ask-1']).toBe(true));
    // One winner renders the bar; the loser neither claims nor releases it.
    expect(screen.getAllByRole('button', { name: 'Allow' })).toHaveLength(1);
    expect(useMcpStore.getState().claimedAsks['ask-1']).toBe(true);
  });

  it('two asks for the same tool spread across two sibling cards', async () => {
    useMcpStore.getState().addAsk(ASK);
    useMcpStore.getState().addAsk({ ...ASK, askId: 'ask-2' });
    render(
      <>
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />
      </>,
    );
    await waitFor(() => {
      const claims = useMcpStore.getState().claimedAsks;
      expect(claims['ask-1']).toBe(true);
      expect(claims['ask-2']).toBe(true);
    });
    expect(screen.getAllByRole('button', { name: 'Allow' })).toHaveLength(2);
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

  it('menu scope "for this conversation" persists the ASK session policy then resolves', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Deny options' }));
    fireEvent.click(screen.getByRole('button', { name: 'Deny for this conversation' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'deny'));
    // The write targets the ask's OWN session id — not whichever session is selected.
    // (waitFor: the decision POST intentionally does not block on the policy write.)
    await waitFor(() =>
      expect(setSessionMcpPolicy).toHaveBeenCalledWith('sess-1', 'mcp-auth-kc', [
        { tool: 'other-tool', verdict: 'allow' },
        { tool: 'list-users', verdict: 'deny' },
      ]),
    );
  });

  it('hides the conversation scope when the ask carries no session id', () => {
    useMcpStore.getState().addAsk({ ...ASK, sessionId: '' });
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Deny options' }));
    expect(screen.queryByRole('button', { name: 'Deny for this conversation' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Deny always (all conversations)' })).toBeInTheDocument();
  });

  it('menu scope "always" persists the global policy then resolves', async () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow options' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow always (all conversations)' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'allow'));
    await waitFor(() =>
      expect(setMyMcpPolicy).toHaveBeenCalledWith('mcp-auth-kc', [
        { tool: '*', verdict: 'ask' },
        { tool: 'list-users', verdict: 'allow' },
      ]),
    );
  });

  it('a failed policy write must not block resolving the held call', async () => {
    vi.mocked(getMyMcpPolicy).mockRejectedValueOnce(new Error('boom'));
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow options' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow always (all conversations)' }));
    await waitFor(() => expect(decideMcpAsk).toHaveBeenCalledWith('ask-1', 'allow'));
    expect(setMyMcpPolicy).not.toHaveBeenCalled();
    expect(useMcpStore.getState().pendingAsks).toHaveLength(0);
  });

  it('a historical card (no isLoading prop) never claims — the banner backstops', async () => {
    useMcpStore.getState().addAsk(ASK);
    // MessageBubble renders persisted tool_use cards with no isLoading at all; results
    // land as separate tool_result cards, so `!result` alone must not make it claim.
    render(
      <>
        <ToolCallCard name="mcp__mcp-auth-kc__list-users" />
        <McpAskNotice sessionId="s1" />
      </>,
    );
    // The card renders no bar (it didn't claim); the fallback backstops with the SAME full
    // decision affordances (scope split-buttons), not a stripped-down prompt.
    expect(screen.getByTestId('mcp-ask-fallback')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Allow options' })).toBeInTheDocument();
    expect(useMcpStore.getState().claimedAsks['ask-1']).toBeUndefined();
  });

  it('closes the scope menu on an outside click', () => {
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Allow options' }));
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument();
  });

  it('a failed decision POST still clears the ask locally (server hold times out)', async () => {
    vi.mocked(decideMcpAsk).mockRejectedValueOnce(new Error('500'));
    useMcpStore.getState().addAsk(ASK);
    render(<ToolCallCard name="mcp__mcp-auth-kc__list-users" isLoading />);
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await waitFor(() => expect(useMcpStore.getState().pendingAsks).toHaveLength(0));
  });
});
