import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelSettingsPage } from './ModelSettingsPage';
import * as adminApi from '@/services/adminApi';

vi.mock('@/services/adminApi');

const OPUS = 'eu.anthropic.claude-opus-4-8';
const HAIKU = 'eu.anthropic.claude-haiku-4-5';
const LLAMA = 'meta.llama3-1-70b-instruct-v1:0';

const SETTINGS: adminApi.ModelSettings = {
  provider: 'bedrock',
  default_model: OPUS,
  default_engine: 'claude-cli',
  enabled_models: [OPUS],
  include_all_vendors: false,
  inkwest_agent_enabled: true,
  engines: ['claude-cli', 'inkwest-agent'],
  catalog: {
    last_refreshed_at: '2026-06-19T10:00:00+00:00',
    source: 'live',
    last_fetch_failed: false,
    count: 3,
  },
  models: [
    {
      id: OPUS,
      label: 'Opus 4.8',
      provider: 'anthropic',
      enabled: true,
      is_default: true,
      source: 'discovered',
      legacy: false,
      priced: true,
      endpoint: null,
      arn: null,
      options: {},
      capable_engines: ['claude-cli', 'inkwest-agent'],
      input_modalities: ['TEXT', 'IMAGE'],
      output_modalities: ['TEXT'],
      input_price: 5.0,
      output_price: 25.0,
    },
    {
      id: HAIKU,
      label: 'Haiku 4.5',
      provider: 'anthropic',
      enabled: false,
      is_default: false,
      source: 'discovered',
      legacy: false,
      priced: true,
      endpoint: null,
      arn: null,
      options: {},
      capable_engines: ['claude-cli', 'inkwest-agent'],
      input_modalities: ['TEXT'],
      output_modalities: ['TEXT'],
      input_price: 1.0,
      output_price: 5.0,
    },
    {
      id: LLAMA,
      label: 'Llama3 1 70B Instruct',
      provider: 'meta',
      enabled: false,
      is_default: false,
      source: 'custom',
      legacy: false,
      priced: false,
      endpoint: null,
      arn: null,
      options: {},
      capable_engines: ['inkwest-agent'],
      input_modalities: ['TEXT'],
      output_modalities: ['TEXT'],
      input_price: null,
      output_price: null,
    },
    {
      // An embedding model (non-chat output) — exercises the non-chat badge + the
      // chat-capable-only filter (P1.8).
      id: 'amazon.titan-embed-text-v2:0',
      label: 'Titan Embed v2',
      provider: 'amazon',
      enabled: false,
      is_default: false,
      source: 'discovered',
      legacy: false,
      priced: true,
      endpoint: null,
      arn: null,
      options: {},
      capable_engines: ['inkwest-agent'],
      input_modalities: ['TEXT'],
      output_modalities: ['EMBEDDING'],
      input_price: 0.02,
      output_price: 0.0,
    },
  ],
};

const mockedApi = vi.mocked(adminApi);

describe('ModelSettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.getModelSettings.mockResolvedValue(structuredClone(SETTINGS));
    // Echo the saved selection the way the real endpoint does — reflect enabled +
    // per-engine availability back into models[] so the page's adopt() stays in sync
    // across successive save-on-click interactions.
    mockedApi.updateModelSettings.mockImplementation(async (req) => {
      const base = structuredClone(SETTINGS);
      base.default_model = req.default_model;
      base.default_engine = req.default_engine ?? base.default_engine;
      base.enabled_models = req.enabled_models;
      base.models = base.models.map((m) => ({
        ...m,
        enabled: req.enabled_models.includes(m.id),
        is_default: m.id === req.default_model,
        options: req.engine_availability?.[m.id]
          ? { ...m.options, engines: req.engine_availability[m.id] }
          : m.options,
      }));
      return base;
    });
    mockedApi.refreshModels.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.addModel.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.updateModel.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.deleteModel.mockResolvedValue(structuredClone(SETTINGS));
  });

  it('renders one section per engine, grouped by provider', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText(`Offer Opus 4.8 on Claude Code`);

    expect(screen.getByText('Claude Code')).toBeInTheDocument();
    expect(screen.getByText('Inkwest Agent')).toBeInTheDocument();
    // A Claude model is offered in both engine sections; Llama (Converse-only) only in Agent.
    expect(screen.getByLabelText('Offer Opus 4.8 on Claude Code')).toBeInTheDocument();
    expect(screen.getByLabelText('Offer Opus 4.8 on Inkwest Agent')).toBeInTheDocument();
    expect(screen.getByLabelText('Offer Llama3 1 70B Instruct on Inkwest Agent')).toBeInTheDocument();
    expect(
      screen.queryByLabelText('Offer Llama3 1 70B Instruct on Claude Code')
    ).not.toBeInTheDocument();
    // The custom non-Anthropic model has no pricing -> "unpriced" + "custom" badges.
    expect(screen.getByText('unpriced')).toBeInTheDocument();
    expect(screen.getByText('custom')).toBeInTheDocument();
  });

  it('filters with the quick search', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText(/Quick search/), 'llama');

    expect(screen.queryByLabelText('Offer Haiku 4.5 on Inkwest Agent')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Offer Llama3 1 70B Instruct on Inkwest Agent')).toBeInTheDocument();
  });

  it('offers a model on an engine and saves per-engine availability', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Haiku 4.5 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Offer Haiku 4.5 on Claude Code'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenCalledWith({
        default_model: OPUS,
        default_engine: 'claude-cli',
        enabled_models: [OPUS, HAIKU],
        engine_availability: {
          [OPUS]: ['claude-cli', 'inkwest-agent'],
          [HAIKU]: ['claude-cli'],
        },
      })
    );
  });

  it('stars an Engine·Model pairing as the new default and saves on click', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Haiku 4.5 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Offer Haiku 4.5 on Claude Code'));
    await user.click(screen.getByLabelText('Set Haiku 4.5 on Claude Code as default'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenLastCalledWith(
        expect.objectContaining({ default_model: HAIKU, default_engine: 'claude-cli' })
      )
    );
  });

  it("does not let a never-offered model's star set the default", async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Haiku 4.5 on Claude Code');
    const user = userEvent.setup();

    // Haiku is offered nowhere on load → its star is disabled and a no-op.
    await user.click(screen.getByLabelText('Set Haiku 4.5 on Claude Code as default'));

    expect(mockedApi.updateModelSettings).not.toHaveBeenCalled();
  });

  it('picks the engine when starring the default (different engine = different default)', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Inkwest Agent');
    const user = userEvent.setup();

    // Opus is offered on both engines on load; star it under Inkwest Agent.
    await user.click(screen.getByLabelText('Set Opus 4.8 on Inkwest Agent as default'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenLastCalledWith(
        expect.objectContaining({ default_model: OPUS, default_engine: 'inkwest-agent' })
      )
    );
  });

  it('reassigns the default when the default model loses every engine', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    // Offer Haiku (so the set survives), then un-offer Opus on both engines: the
    // default (Opus) is gone, so it falls back to the surviving Haiku.
    await user.click(screen.getByLabelText('Offer Haiku 4.5 on Claude Code'));
    await user.click(screen.getByLabelText('Offer Opus 4.8 on Claude Code'));
    await user.click(screen.getByLabelText('Offer Opus 4.8 on Inkwest Agent'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenLastCalledWith({
        default_model: HAIKU,
        default_engine: 'claude-cli',
        enabled_models: [HAIKU],
        engine_availability: { [HAIKU]: ['claude-cli'] },
      })
    );
  });

  it('enables a whole engine section with its "All" control', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Offer all on Claude Code'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          enabled_models: expect.arrayContaining([OPUS, HAIKU]),
          engine_availability: expect.objectContaining({ [HAIKU]: ['claude-cli'] }),
        })
      )
    );
  });

  it('disables an engine section with its "None" control', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Inkwest Agent');
    const user = userEvent.setup();

    // "None" on the Inkwest Agent section removes that engine from every model;
    // Opus survives on Claude Code, so the set stays non-empty.
    await user.click(screen.getByLabelText('Offer none on Inkwest Agent'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenCalledWith({
        default_model: OPUS,
        default_engine: 'claude-cli',
        enabled_models: [OPUS],
        engine_availability: { [OPUS]: ['claude-cli'] },
      })
    );
  });

  it('collapses an engine section, hiding its rows', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    // The section header toggle is labelled by aria-expanded; click the Claude Code one.
    const claudeToggle = screen.getByRole('button', { expanded: true, name: /Claude Code/ });
    await user.click(claudeToggle);

    expect(screen.queryByLabelText('Offer Opus 4.8 on Claude Code')).not.toBeInTheDocument();
    // The Agent section is still expanded.
    expect(screen.getByLabelText('Offer Opus 4.8 on Inkwest Agent')).toBeInTheDocument();
  });

  it('refreshes the catalog when toggling all-vendor models', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText(/Include all Bedrock vendors/));

    await waitFor(() => expect(mockedApi.refreshModels).toHaveBeenCalledWith(true));
  });

  it('adds a custom model through the modal', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /Add model/ }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Model ID'), 'mistral.large-v1:0');
    await user.click(within(dialog).getByRole('button', { name: 'Add model' }));

    await waitFor(() =>
      expect(mockedApi.addModel).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'mistral.large-v1:0', provider: 'bedrock' })
      )
    );
  });

  it('edits a model through the modal', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Edit Opus 4.8 (Claude Code section)'));
    const dialog = await screen.findByRole('dialog');
    const nameInput = within(dialog).getByLabelText('Display name (optional)');
    await user.clear(nameInput);
    await user.type(nameInput, 'House Opus');
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(mockedApi.updateModel).toHaveBeenCalledWith(
        OPUS,
        expect.objectContaining({ display_name: 'House Opus' })
      )
    );
  });

  it('deletes a custom model after confirmation', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Delete Llama3 1 70B Instruct (Inkwest Agent section)'));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(mockedApi.deleteModel).toHaveBeenCalledWith(LLAMA));
  });

  it('shows modality icons + a non-chat badge per model (P1.8)', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');

    // Opus accepts image input → IMAGE icon (appears in both engine sections).
    expect(screen.getAllByLabelText('IMAGE — image input').length).toBeGreaterThan(0);
    // Haiku is text-only → no IMAGE icon associated with its rows is required; the
    // embedding model has non-TEXT output → a "non-chat" badge.
    expect(screen.getByText('non-chat')).toBeInTheDocument();
  });

  it('input checkboxes (Text + modalities) are pre-checked, hiding nothing by default', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');

    expect(screen.getByLabelText('Show text-only input models')).toBeChecked();
    for (const mod of ['IMAGE', 'VIDEO', 'SPEECH']) {
      expect(screen.getByLabelText(`Show ${mod} input models`)).toBeChecked();
    }
    expect(screen.getByLabelText('Offer Opus 4.8 on Inkwest Agent')).toBeInTheDocument();
  });

  it('unchecking IMAGE hides image-only models but keeps text-only ones', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Show IMAGE input models'));

    // Opus (TEXT+IMAGE) loses its only badged modality → hidden everywhere; the
    // text-only Haiku/Llama still show (their TEXT category is still allowed).
    expect(screen.queryByLabelText('Offer Opus 4.8 on Inkwest Agent')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Offer Haiku 4.5 on Inkwest Agent')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Offer Llama3 1 70B Instruct on Inkwest Agent')
    ).toBeInTheDocument();
  });

  it('unchecking Text hides text-only models but keeps multimodal ones', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Show text-only input models'));

    // Haiku/Llama (text-only) drop out; Opus (has IMAGE) stays — symmetric with the
    // output facet, so unchecking every input box would hide everything.
    expect(screen.queryByLabelText('Offer Haiku 4.5 on Inkwest Agent')).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText('Offer Llama3 1 70B Instruct on Inkwest Agent')
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('Offer Opus 4.8 on Inkwest Agent')).toBeInTheDocument();
  });

  it('unchecking Non-chat hides embedding / image-gen output models', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    // Output toggles are pre-checked, so the embedding model shows initially.
    expect(screen.getByLabelText('Show chat models')).toBeChecked();
    expect(screen.getByLabelText('Show non-chat models')).toBeChecked();
    expect(screen.getByText('non-chat')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Show non-chat models'));

    // The embedding model is now hidden; the non-chat badge is gone with it.
    expect(screen.queryByText('non-chat')).not.toBeInTheDocument();
    // Chat models remain.
    expect(screen.getByLabelText('Offer Opus 4.8 on Claude Code')).toBeInTheDocument();
  });

  it('unchecking Chat leaves only non-chat output models', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Show chat models'));

    // Chat models drop out; the embedding (non-chat) model and its badge remain.
    expect(screen.queryByLabelText('Offer Opus 4.8 on Claude Code')).not.toBeInTheDocument();
    expect(screen.getByText('non-chat')).toBeInTheDocument();
  });

  it('hides the all-vendors toggle when the inkwest-agent engine is disabled', async () => {
    mockedApi.getModelSettings.mockResolvedValue({
      ...structuredClone(SETTINGS),
      inkwest_agent_enabled: false,
      engines: ['claude-cli'],
    });
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Offer Opus 4.8 on Claude Code');

    expect(screen.queryByLabelText(/Include all Bedrock vendors/)).not.toBeInTheDocument();
  });
});
