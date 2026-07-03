import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { McpToolPolicyEditor } from './McpToolPolicyEditor';
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

  it('degrades gracefully when enumeration fails: error + default control still renders', async () => {
    mGetTools.mockRejectedValue(new Error('500'));
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);
    expect(await screen.findByText(/couldn't list this server's tools/i)).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Permission for server default' })).toBeInTheDocument();
  });
});

describe('McpToolPolicyEditor — session scope (5-segment slider)', () => {
  it('a conversation override wins over the global default (pressed segment)', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'allow' }] });
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    // conv deny shadows global allow → "Deny this conversation" is pressed.
    const seg = await screen.findByRole('button', { name: 'Deny this conversation — query' });
    expect(seg).toHaveAttribute('aria-pressed', 'true');
  });

  it('"always allow" writes the global default AND clears the conversation override', async () => {
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Always allow — query' }));
    await waitFor(() => expect(mSetGlobal).toHaveBeenCalled());
    expect(mSetGlobal.mock.calls[0][1]).toContainEqual({ tool: 'query', verdict: 'allow' });
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2].find((e) => e.tool === 'query')).toBeUndefined();
  });

  it('"deny this conversation" writes only the conversation row', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Deny this conversation — query' }));
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2]).toContainEqual({ tool: 'query', verdict: 'deny' });
    expect(mSetGlobal).not.toHaveBeenCalled();
  });

  it('defaults an unset tool to "ask" (pressed segment)', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    const seg = await screen.findByRole('button', { name: 'Ask each time — query' });
    expect(seg).toHaveAttribute('aria-pressed', 'true');
  });
});
