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
  return { key: 'k', tools: names.map((n) => ({ name: n, description: '', input_schema: {} })), fetched_at: null };
}

beforeEach(() => {
  vi.clearAllMocks();
  mGetTools.mockResolvedValue(tools('query'));
  mGetGlobal.mockResolvedValue({ policies: [] });
  mGetConv.mockResolvedValue({ policies: [] });
  mSetGlobal.mockResolvedValue({ policies: [] });
  mSetConv.mockResolvedValue({ policies: [] });
});

describe('McpToolPolicyEditor — global scope', () => {
  it('renders the current global verdict and writes the changed verdict', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="global" />);

    const select = (await screen.findByLabelText('Permission for query')) as HTMLSelectElement;
    expect(select.value).toBe('deny');

    fireEvent.change(select, { target: { value: 'allow' } });
    await waitFor(() => expect(mSetGlobal).toHaveBeenCalled());
    const [serverKey, entries] = mSetGlobal.mock.calls[0];
    expect(serverKey).toBe('k');
    expect(entries).toContainEqual({ tool: 'query', verdict: 'allow' });
    expect(mSetConv).not.toHaveBeenCalled(); // global scope never touches conversation rows
  });
});

describe('McpToolPolicyEditor — session scope', () => {
  it('a conversation override wins over the global default in the derived choice', async () => {
    mGetGlobal.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'allow' }] });
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);

    const select = (await screen.findByLabelText('Permission for query')) as HTMLSelectElement;
    expect(select.value).toBe('conv_deny'); // conv deny shadows global allow
  });

  it('"always allow" writes the global default AND clears the conversation override', async () => {
    mGetConv.mockResolvedValue({ policies: [{ server_key: 'k', tool: 'query', verdict: 'deny' }] });
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);

    const select = await screen.findByLabelText('Permission for query');
    fireEvent.change(select, { target: { value: 'always_allow' } });

    await waitFor(() => expect(mSetGlobal).toHaveBeenCalled());
    expect(mSetGlobal.mock.calls[0][1]).toContainEqual({ tool: 'query', verdict: 'allow' });
    // conv override for the tool must be cleared so it can't shadow the new global default.
    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    const convEntries = mSetConv.mock.calls[0][2];
    expect(convEntries.find((e) => e.tool === 'query')).toBeUndefined();
  });

  it('"deny this conversation" writes only the conversation row', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    const select = await screen.findByLabelText('Permission for query');
    fireEvent.change(select, { target: { value: 'conv_deny' } });

    await waitFor(() => expect(mSetConv).toHaveBeenCalled());
    expect(mSetConv.mock.calls[0][2]).toContainEqual({ tool: 'query', verdict: 'deny' });
    expect(mSetGlobal).not.toHaveBeenCalled();
  });

  it('defaults an unset tool to "ask"', async () => {
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);
    const select = (await screen.findByLabelText('Permission for query')) as HTMLSelectElement;
    expect(select.value).toBe('ask');
  });

  it('locks every dropdown while a save is in flight (no lost-update race)', async () => {
    mGetTools.mockResolvedValue(tools('query', 'search'));
    let resolveSave: (v: { policies: [] }) => void = () => {};
    mSetConv.mockReturnValue(new Promise((r) => (resolveSave = r)));
    render(<McpToolPolicyEditor serverKey="k" scope="session" sessionId="s1" />);

    const query = await screen.findByLabelText('Permission for query');
    fireEvent.change(query, { target: { value: 'conv_deny' } });
    // While the PUT is pending, a different tool's dropdown must be disabled too.
    await waitFor(() =>
      expect(screen.getByLabelText('Permission for search')).toBeDisabled()
    );
    resolveSave({ policies: [] });
    await waitFor(() =>
      expect(screen.getByLabelText('Permission for search')).not.toBeDisabled()
    );
  });
});
