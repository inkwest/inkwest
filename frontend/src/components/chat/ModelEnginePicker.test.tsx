import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelEnginePicker } from './ModelEnginePicker';
import type { AppConfig } from '@/services/api';

const CONFIG: AppConfig = {
  provider: 'claude-cli',
  model: 'claude-sonnet-4-6',
  default_engine: 'claude-cli',
  engines: ['claude-cli', 'inkwest-agent'],
  available_models: [
    { id: 'claude-sonnet-4-6', label: 'Sonnet 4.6', legacy: false, engines: ['claude-cli', 'inkwest-agent'] },
    { id: 'meta.llama3', label: 'Llama 3', legacy: false, engines: ['inkwest-agent'] },
  ],
  attachments_enabled: false,
};

describe('ModelEnginePicker', () => {
  it('checks both the Default row and the engine·model pair it resolves to', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    // Unpinned (model null): the default resolves to claude-cli · Sonnet 4.6.
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={onChange} />);
    // The collapsed trigger shows the resolved pairing (no "Default —" prefix).
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));

    const listbox = screen.getByRole('listbox');
    const opts = within(listbox).getAllByRole('option');
    const text = (o: HTMLElement) => o.textContent ?? '';
    const defaultRow = opts.find((o) => text(o).startsWith('Default'));
    // The concrete pairing the default runs (Claude Code · Sonnet 4.6, not the Default row).
    const ccPair = opts.find((o) => !text(o).startsWith('Default') && text(o).includes('Claude Code · Sonnet 4.6'));
    const iaePair = opts.find((o) => text(o).includes('Inkwest Agent · Sonnet 4.6'));

    expect(defaultRow).toHaveAttribute('aria-selected', 'true');
    expect(ccPair).toHaveAttribute('aria-selected', 'true');
    // A different pairing of the same model is not.
    expect(iaePair).toHaveAttribute('aria-selected', 'false');
  });

  it('emits the chosen (model, engine) pair on selection', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={onChange} />);
    // The collapsed trigger shows the resolved pairing (no "Default —" prefix).
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));
    await user.click(screen.getByRole('option', { name: /Inkwest Agent · Llama 3/ }));
    expect(onChange).toHaveBeenCalledWith('meta.llama3', 'inkwest-agent');
  });

  it('keeps a since-disabled pinned model selectable when allowDisabledCurrent is set', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <ModelEnginePicker
        config={CONFIG}
        model="claude-retired-9"
        engine="inkwest-agent"
        onChange={onChange}
        allowDisabledCurrent
      />
    );
    await user.click(screen.getByRole('button', { name: /claude-retired-9/ }));
    expect(screen.getByRole('option', { name: /claude-retired-9 \(disabled\)/ })).toBeInTheDocument();
  });
});
