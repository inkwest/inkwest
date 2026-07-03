import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { McpToolPolicyEditor } from './McpToolPolicyEditor';
import { useMcpPolicyVersion } from '@/store/mcpPolicy';
import * as api from '@/services/api';

vi.mock('@/services/api', () => ({
  getMcpServerTools: vi.fn(),
  getMyMcpPolicy: vi.fn(),
  getSessionMcpPolicy: vi.fn(),
  setMyMcpPolicy: vi.fn(),
  setSessionMcpPolicy: vi.fn(),
}));

const mGetTools = vi.mocked(api.getMcpServerTools);
const mGetGlobal = vi.mocked(api.getMyMcpPolicy);
const mGetConv = vi.mocked(api.getSessionMcpPolicy);
const mSetGlobal = vi.mocked(api.setMyMcpPolicy);
const mSetConv = vi.mocked(api.setSessionMcpPolicy);

function tools(...names: string[]) {
  return {
    key: 'k',
    fetched_at: null,
    tools: names.map((n) => ({ name: n, description: '', input_schema: {} })),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mGetTools.mockResolvedValue(tools('query'));
  mGetGlobal.mockResolvedValue({ policies: [] });
  mGetConv.mockResolvedValue({ policies: [] });
  mSetGlobal.mockResolvedValue({ policies: [] });
  mSetConv.mockResolvedValue({ policies: [] });
});

describe('McpToolPolicyEditor — global scope (segmented slider)', () => {
  it('shows the current verdict as the pressed segment and writes the change', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);

    const deny = await screen.findByRole('button', { name: 'Deny query' });
    expect(deny).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByRole('button', { name: 'Allow query' }));
    await waitFor(() => expect(mSetGlobal).toHaveBeenCalled());
    const [serverKey, entries] = mSetGlobal.mock.calls[0];
    expect(serverKey).toBe('k');
    expect(entries).toContainEqual({ tool: 'query', verdict: 'allow' });
    expect(mSetConv).not.toHaveBeenCalled(); // global scope never touches conversation rows
  });

  it('updates optimistically — the new segment is pressed before the PUT resolves', async () => {
    let pending = true;
    mSetGlobal.mockReturnValue(new Promise(() => (pending = true))); // never resolves
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    const deny = await screen.findByRole('button', { name: 'Deny query' });
    fireEvent.click(deny);
    // No await on the PUT: the UI reflects the choice immediately (no disable/flicker).
    expect(screen.getByRole('button', { name: 'Deny query' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Allow query' })).not.toBeDisabled();
    expect(pending).toBe(true);
  });

  it('filters tools by the search box', async () => {
    mGetTools.mockResolvedValue(tools('query_dataprime', 'search_logs'));
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    await screen.findByRole('group', { name: 'Permission for query_dataprime' });
    fireEvent.change(screen.getByLabelText('Search tools'), { target: { value: 'search' } });
    expect(screen.queryByRole('group', { name: 'Permission for query_dataprime' })).not.toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Permission for search_logs' })).toBeInTheDocument();
  });

  it('reloads (force re-enumeration) on the reload button', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    await screen.findByRole('button', { name: /reload/i });
    mGetTools.mockClear();
    fireEvent.click(screen.getByRole('button', { name: /reload/i }));
    await waitFor(() => expect(mGetTools).toHaveBeenCalledWith('k', true));
  });

  it('degrades gracefully when enumeration fails: error + bulk actions still render', async () => {
    mGetTools.mockRejectedValue(new Error('500'));
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    expect(await screen.findByText(/couldn't list this server's tools/i)).toBeInTheDocument();
    // Bulk actions still render (they apply to future tools via the '*' default too).
    expect(screen.getByRole('button', { name: 'Allow all' })).toBeInTheDocument();
  });

  it('"Deny all" sets every tool + the default to deny in one save', async () => {
    mGetTools.mockResolvedValue(tools('a', 'b'));
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Deny all' }));
    await waitFor(() => expect(mSetGlobal).toHaveBeenCalled());
    const entries = mSetGlobal.mock.calls[0][1];
    // every tool + the '*' default, all denied
    expect(entries).toContainEqual({ tool: 'a', verdict: 'deny' });
    expect(entries).toContainEqual({ tool: 'b', verdict: 'deny' });
    expect(entries).toContainEqual({ tool: '*', verdict: 'deny' });
  });
});

describe('McpToolPolicyEditor — session scope (inherit / local override + reset)', () => {
  it('with no override, shows the INHERITED global verdict and reset is disabled', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    mGetConv.mockResolvedValue({ policies: [] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    // Inherits global 'deny' → Deny pressed; no local override → reset disabled.
    expect(await screen.findByRole('button', { name: 'Deny query' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Reset query to inherited' })).toBeDisabled();
  });

  it('inherits the server default (*) when the tool has no global row', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: '*', verdict: 'allow' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    expect(await screen.findByRole('button', { name: 'Allow query' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('a local override wins over the inherited default and enables reset', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'allow' }] });
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    expect(await screen.findByRole('button', { name: 'Deny query' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Reset query to inherited' })).not.toBeDisabled();
  });

  it('changing a tool writes ONLY a conversation override (never global)', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Deny query' }));
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2]).toContainEqual({ tool: 'query', verdict: 'deny' });
    expect(mSetGlobal).not.toHaveBeenCalled();
  });

  it('reset removes the conversation override (reverts to inherited)', async () => {
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Reset query to inherited' }));
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2].find((e) => e.tool === 'query')).toBeUndefined();
    expect(mSetGlobal).not.toHaveBeenCalled();
  });

  it('"Reset all" clears every conversation override (reverts all to inherited)', async () => {
    mGetTools.mockResolvedValue(tools('a', 'b'));
    mGetConv.mockResolvedValue({
      policies: [
        { server_key: 'k', tool: 'a', verdict: 'deny' },
        { server_key: 'k', tool: 'b', verdict: 'allow' },
      ],
    });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: /reset all/i }));
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2]).toEqual([]); // full-state replace with nothing → all inherit
    expect(mSetGlobal).not.toHaveBeenCalled();
  });

  it('"Reset all" is not offered in the global settings scope', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    await screen.findByRole('button', { name: 'Allow all' });
    expect(screen.queryByRole('button', { name: /reset all/i })).not.toBeInTheDocument();
  });

  it('re-fetches the global policy when it changes elsewhere (stacked settings edit)', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    await screen.findByRole('button', { name: 'Ask query' }); // initial load done
    const before = mGetGlobal.mock.calls.length;
    act(() => useMcpPolicyVersion.getState().bumpGlobal()); // a global-scope write happened above
    await waitFor(() => expect(mGetGlobal.mock.calls.length).toBeGreaterThan(before));
  });

  it('"Deny all" in a conversation writes conv-scope deny for tools, NOT the server default', async () => {
    mGetTools.mockResolvedValue(tools('a', 'b'));
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Deny all' }));
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    const entries = mSetConv.mock.calls[0][2];
    expect(entries).toContainEqual({ tool: 'a', verdict: 'deny' });
    // No conv '*' — the conversation view has no server-default; it would silently enforce on
    // later-discovered tools while the UI shows them inheriting the global default.
    expect(entries.find((e) => e.tool === '*')).toBeUndefined();
    expect(mSetGlobal).not.toHaveBeenCalled();
  });
});
