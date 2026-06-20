import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SessionModelSelect } from './SessionModelSelect';
import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';

const SONNET = { id: 'claude-sonnet-4-20250514', label: 'Sonnet 4.0', legacy: true };
const OPUS = { id: 'claude-opus-4-20250514', label: 'Opus 4.0', legacy: true };
const LLAMA = { id: 'meta.llama3-70b', label: 'Llama 3 70B', legacy: false };

// Single-engine deployment: plain model names, no engine choice.
const CONFIG_CLI = {
  provider: 'anthropic',
  model: 'claude-sonnet-4-20250514',
  default_engine: 'claude-cli',
  engines: ['claude-cli'],
  available_models: [
    { ...SONNET, engines: ['claude-cli'] },
    { ...OPUS, engines: ['claude-cli'] },
  ],
  attachments_enabled: false,
};

// Both engines on: Engine·Model pairs. Claude → both engines, Llama → agent only.
const CONFIG_BOTH = {
  provider: 'claude-cli',
  model: 'claude-sonnet-4-20250514',
  default_engine: 'claude-cli',
  engines: ['claude-cli', 'inkwest-agent'],
  available_models: [
    { ...SONNET, engines: ['claude-cli', 'inkwest-agent'] },
    { ...OPUS, engines: ['claude-cli', 'inkwest-agent'] },
    { ...LLAMA, engines: ['inkwest-agent'] },
  ],
  attachments_enabled: false,
};

function setSession(model: string | null, engine: string | null = null) {
  useSessionsStore.setState({
    currentSession: {
      id: 's1',
      cluster_id: null,
      model,
      engine,
      cluster: null,
      messages: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    },
  });
}

const trigger = () => screen.getByTitle('Provider: anthropic · Model for this conversation');
const triggerBoth = () => screen.getByTitle('Provider: claude-cli · Model for this conversation');

describe('SessionModelSelect', () => {
  beforeEach(() => {
    useConfigStore.setState({ config: CONFIG_CLI, fetchConfig: vi.fn() });
    useSessionsStore.setState({ currentSession: null, setSessionModel: vi.fn() });
  });

  it('renders nothing without an active session', () => {
    render(<SessionModelSelect />);
    expect(screen.queryByTitle(/Provider:/)).not.toBeInTheDocument();
  });

  it('shows the Default entry when unpinned and changes the model on the fly', async () => {
    const setSessionModel = vi.fn().mockResolvedValue(undefined);
    useSessionsStore.setState({ setSessionModel });
    setSession(null);
    const user = userEvent.setup();
    render(<SessionModelSelect />);

    // Collapsed trigger drops the "Default —" prefix (shown only in the dropdown row).
    expect(trigger()).toHaveTextContent('Sonnet 4.0');
    await user.click(trigger());
    expect(screen.getByRole('option', { name: /Default — Sonnet 4.0/ })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: /Opus 4.0/ }));
    // Single engine ⇒ engine is left null (backend resolves per turn).
    expect(setSessionModel).toHaveBeenCalledWith('s1', 'claude-opus-4-20250514', null);
  });

  it('un-pins the session when Default is selected', async () => {
    const setSessionModel = vi.fn().mockResolvedValue(undefined);
    useSessionsStore.setState({ setSessionModel });
    setSession('claude-opus-4-20250514');
    const user = userEvent.setup();
    render(<SessionModelSelect />);

    await user.click(trigger());
    await user.click(screen.getByRole('option', { name: /Default/ }));
    expect(setSessionModel).toHaveBeenCalledWith('s1', null, null);
  });

  it('shows the session model when one is pinned', () => {
    setSession('claude-opus-4-20250514');
    render(<SessionModelSelect />);
    expect(trigger()).toHaveTextContent('Opus 4.0');
  });

  it('keeps a since-disabled pinned model displayable, marked disabled', async () => {
    setSession('claude-retired-1');
    const user = userEvent.setup();
    render(<SessionModelSelect />);
    expect(trigger()).toHaveTextContent('claude-retired-1');
    await user.click(trigger());
    expect(screen.getByRole('option', { name: /claude-retired-1 \(disabled\)/ })).toBeInTheDocument();
  });

  it('offers Engine·Model pairs when both engines are enabled', async () => {
    useConfigStore.setState({ config: CONFIG_BOTH, fetchConfig: vi.fn() });
    const setSessionModel = vi.fn().mockResolvedValue(undefined);
    useSessionsStore.setState({ setSessionModel });
    setSession(null);
    const user = userEvent.setup();
    render(<SessionModelSelect />);

    await user.click(triggerBoth());
    // Claude appears under both engines; Llama only under the agent.
    expect(screen.getByRole('option', { name: /Claude Code · Opus 4.0/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Agent · Opus 4.0/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Agent · Llama 3 70B/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Claude Code · Llama 3 70B/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('option', { name: /Agent · Opus 4.0/ }));
    expect(setSessionModel).toHaveBeenCalledWith('s1', 'claude-opus-4-20250514', 'inkwest-agent');
  });

  it('shows the engine in the trigger when a pairing is pinned', () => {
    useConfigStore.setState({ config: CONFIG_BOTH, fetchConfig: vi.fn() });
    setSession('claude-opus-4-20250514', 'inkwest-agent');
    render(<SessionModelSelect />);
    expect(triggerBoth()).toHaveTextContent('Agent · Opus 4.0');
  });

  it('filters the list with the search box', async () => {
    useConfigStore.setState({ config: CONFIG_BOTH, fetchConfig: vi.fn() });
    setSession(null);
    const user = userEvent.setup();
    render(<SessionModelSelect />);

    await user.click(triggerBoth());
    await user.type(screen.getByPlaceholderText('Search models…'), 'llama');
    const list = screen.getByRole('listbox');
    expect(within(list).getByRole('option', { name: /Llama 3 70B/ })).toBeInTheDocument();
    expect(within(list).queryByRole('option', { name: /Opus 4.0/ })).not.toBeInTheDocument();
    // Default stays reachable regardless of the query.
    expect(within(list).getByRole('option', { name: /Default/ })).toBeInTheDocument();
  });
});
