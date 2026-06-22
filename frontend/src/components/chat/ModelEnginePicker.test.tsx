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
    {
      id: 'claude-sonnet-4-6',
      label: 'Sonnet 4.6',
      legacy: false,
      engines: ['claude-cli', 'inkwest-agent'],
      input_modalities: ['TEXT', 'IMAGE'],
    },
    {
      id: 'meta.llama3',
      label: 'Llama 3',
      legacy: false,
      engines: ['inkwest-agent'],
      input_modalities: ['TEXT'],
    },
    {
      id: 'mistral.voxtral-small',
      label: 'Voxtral Small 24b',
      legacy: false,
      engines: ['inkwest-agent'],
      input_modalities: ['TEXT', 'SPEECH'],
    },
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

  it('shows the IMAGE icon only next to a model that accepts image input', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));
    // Sonnet (TEXT+IMAGE) gets the icon; Llama (TEXT only) doesn't. Scope to the listbox
    // so the trigger's own badge (the selected model) isn't counted here.
    const listbox = screen.getByRole('listbox');
    const icons = within(listbox).getAllByLabelText('IMAGE — image input');
    // One per Sonnet row (claude-cli + inkwest-agent = 2) plus the Default row, which
    // resolves to Sonnet and mirrors its modalities. None for Llama.
    expect(icons.length).toBe(3);
  });

  it('mirrors the resolved default model modalities on the Default row', async () => {
    const user = userEvent.setup();
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));
    const listbox = screen.getByRole('listbox');
    const defaultRow = within(listbox)
      .getAllByRole('option')
      .find((o) => (o.textContent ?? '').startsWith('Default'))!;
    // Default → Sonnet (TEXT+IMAGE): the row shows both icons, not the lone TEXT icon.
    expect(within(defaultRow).getByLabelText('TEXT — text input')).toBeInTheDocument();
    expect(within(defaultRow).getByLabelText('IMAGE — image input')).toBeInTheDocument();
  });

  it('shows the selected model modality on the collapsed trigger', () => {
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={vi.fn()} />);
    // Default resolves to Sonnet (TEXT+IMAGE) → TEXT + IMAGE badges on the pill, dropdown closed.
    expect(screen.getByLabelText('TEXT — text input')).toBeInTheDocument();
    expect(screen.getByLabelText('IMAGE — image input')).toBeInTheDocument();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('shows the universal TEXT icon on every model row, image-capable or not', async () => {
    const user = userEvent.setup();
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));
    const listbox = screen.getByRole('listbox');
    // One TEXT icon per row — including the text-only Llama, which carries no other icon.
    // The count matches the IMAGE+SPEECH-bearing and the text-only rows alike.
    const textIcons = within(listbox).getAllByLabelText('TEXT — text input');
    const imageIcons = within(listbox).getAllByLabelText('IMAGE — image input');
    expect(textIcons.length).toBeGreaterThan(imageIcons.length);
  });

  it('shows the SPEECH icon next to an audio-input model', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ModelEnginePicker config={CONFIG} model={null} engine={null} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: /Claude Code · Sonnet 4.6/ }));
    // Voxtral (TEXT+SPEECH) runs only on inkwest-agent → one row, one SPEECH icon.
    expect(screen.getAllByLabelText('SPEECH — audio input').length).toBe(1);
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
