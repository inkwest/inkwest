import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SessionModelSelect } from './SessionModelSelect';
import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';

const CONFIG = {
  provider: 'anthropic',
  model: 'claude-sonnet-4-20250514',
  available_models: [
    { id: 'claude-sonnet-4-20250514', label: 'Sonnet 4.0', legacy: true },
    { id: 'claude-opus-4-20250514', label: 'Opus 4.0', legacy: true },
  ],
};

function setSession(model: string | null) {
  useSessionsStore.setState({
    currentSession: {
      id: 's1',
      cluster_id: null,
      model,
      cluster: null,
      messages: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    },
  });
}

describe('SessionModelSelect', () => {
  beforeEach(() => {
    useConfigStore.setState({ config: CONFIG, fetchConfig: vi.fn() });
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

    const select = screen.getByTitle('Provider: anthropic · Model for this conversation');
    // Unpinned session: the "Default" entry (with the default's label) is selected
    expect(select).toHaveValue('');
    expect(screen.getByText('Default — Sonnet 4.0')).toBeInTheDocument();

    await user.selectOptions(select, 'claude-opus-4-20250514');
    expect(setSessionModel).toHaveBeenCalledWith('s1', 'claude-opus-4-20250514');
  });

  it('un-pins the session when Default is selected', async () => {
    const setSessionModel = vi.fn().mockResolvedValue(undefined);
    useSessionsStore.setState({ setSessionModel });
    setSession('claude-opus-4-20250514');
    const user = userEvent.setup();
    render(<SessionModelSelect />);

    const select = screen.getByTitle('Provider: anthropic · Model for this conversation');
    await user.selectOptions(select, '');
    expect(setSessionModel).toHaveBeenCalledWith('s1', null);
  });

  it('shows the session model when one is pinned', () => {
    setSession('claude-opus-4-20250514');
    render(<SessionModelSelect />);
    expect(screen.getByTitle('Provider: anthropic · Model for this conversation')).toHaveValue(
      'claude-opus-4-20250514',
    );
  });

  it('keeps a since-disabled pinned model displayable, marked disabled', () => {
    setSession('claude-retired-1');
    render(<SessionModelSelect />);
    const select = screen.getByTitle('Provider: anthropic · Model for this conversation');
    expect(select).toHaveValue('claude-retired-1');
    // Marked so the user knows the backend runs the default instead
    expect(screen.getByText('claude-retired-1 (disabled)')).toBeInTheDocument();
  });
});
