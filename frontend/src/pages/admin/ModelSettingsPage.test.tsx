import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelSettingsPage } from './ModelSettingsPage';
import * as adminApi from '@/services/adminApi';

vi.mock('@/services/adminApi');

const SETTINGS: adminApi.ModelSettings = {
  provider: 'bedrock',
  default_model: 'eu.anthropic.claude-opus-4-8',
  enabled_models: ['eu.anthropic.claude-opus-4-8'],
  include_open: false,
  catalog: {
    last_refreshed_at: '2026-06-19T10:00:00+00:00',
    source: 'live',
    last_fetch_failed: false,
    count: 3,
  },
  models: [
    {
      id: 'eu.anthropic.claude-opus-4-8',
      label: 'Opus 4.8',
      provider: 'bedrock',
      enabled: true,
      is_default: true,
      source: 'discovered',
      legacy: false,
      priced: true,
      endpoint: null,
      arn: null,
      options: {},
      input_price: 5.0,
      output_price: 25.0,
    },
    {
      id: 'eu.anthropic.claude-haiku-4-5',
      label: 'Haiku 4.5',
      provider: 'bedrock',
      enabled: false,
      is_default: false,
      source: 'discovered',
      legacy: false,
      priced: true,
      endpoint: null,
      arn: null,
      options: {},
      input_price: 1.0,
      output_price: 5.0,
    },
    {
      id: 'meta.llama3-1-70b-instruct-v1:0',
      label: 'Llama3 1 70B Instruct',
      provider: 'bedrock-open',
      enabled: false,
      is_default: false,
      source: 'custom',
      legacy: false,
      priced: false,
      endpoint: null,
      arn: null,
      options: {},
      input_price: null,
      output_price: null,
    },
  ],
};

const mockedApi = vi.mocked(adminApi);

describe('ModelSettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.getModelSettings.mockResolvedValue(structuredClone(SETTINGS));
    // Echo the saved selection (the real endpoint returns the new state) so the
    // page's post-save adopt() reflects what was just persisted.
    mockedApi.updateModelSettings.mockImplementation(async (req) => ({
      ...structuredClone(SETTINGS),
      default_model: req.default_model,
      enabled_models: req.enabled_models,
    }));
    mockedApi.refreshModels.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.addModel.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.updateModel.mockResolvedValue(structuredClone(SETTINGS));
    mockedApi.deleteModel.mockResolvedValue(structuredClone(SETTINGS));
  });

  it('groups models by provider and flags unpriced ones red', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');

    expect(screen.getByText('Bedrock (Claude)')).toBeInTheDocument();
    expect(screen.getByText('Bedrock (open-weight)')).toBeInTheDocument();
    // The custom open-weight model has no pricing -> "unpriced" + "custom" badges
    expect(screen.getByText('unpriced')).toBeInTheDocument();
    expect(screen.getByText('custom')).toBeInTheDocument();
  });

  it('filters with the quick search', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText(/Quick search/), 'llama');

    expect(screen.queryByLabelText('Enable Haiku 4.5')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Enable Llama3 1 70B Instruct')).toBeInTheDocument();
  });

  it('saves immediately when enabling a model (no Save button)', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Haiku 4.5');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Enable Haiku 4.5'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenCalledWith({
        default_model: 'eu.anthropic.claude-opus-4-8',
        enabled_models: ['eu.anthropic.claude-opus-4-8', 'eu.anthropic.claude-haiku-4-5'],
      })
    );
    expect(screen.queryByRole('button', { name: 'Save selection' })).not.toBeInTheDocument();
  });

  it('stars an enabled model as the new default and saves on click', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Haiku 4.5');
    const user = userEvent.setup();

    // Enable Haiku (saves), then star it — the star persists on click.
    await user.click(screen.getByLabelText('Enable Haiku 4.5'));
    await screen.findByLabelText('Set Haiku 4.5 as default');
    await user.click(screen.getByLabelText('Set Haiku 4.5 as default'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenLastCalledWith({
        default_model: 'eu.anthropic.claude-haiku-4-5',
        enabled_models: ['eu.anthropic.claude-opus-4-8', 'eu.anthropic.claude-haiku-4-5'],
      })
    );
  });

  it("does not let a disabled model's star set the default", async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Haiku 4.5');
    const user = userEvent.setup();

    // Haiku is disabled on load → its star is disabled and a no-op.
    await user.click(screen.getByLabelText('Set Haiku 4.5 as default'));

    expect(mockedApi.updateModelSettings).not.toHaveBeenCalled();
  });

  it('reassigns the default when the current default is disabled', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Haiku 4.5');
    const user = userEvent.setup();

    // Enable Haiku, then disable Opus (the current default): the default falls
    // back to the remaining enabled model and saves automatically.
    await user.click(screen.getByLabelText('Enable Haiku 4.5'));
    await user.click(screen.getByLabelText('Enable Opus 4.8'));

    await waitFor(() =>
      expect(mockedApi.updateModelSettings).toHaveBeenLastCalledWith({
        default_model: 'eu.anthropic.claude-haiku-4-5',
        enabled_models: ['eu.anthropic.claude-haiku-4-5'],
      })
    );
  });

  it('refreshes the catalog when toggling open-weight models', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText(/Include open-weight/));

    await waitFor(() => expect(mockedApi.refreshModels).toHaveBeenCalledWith(true));
  });

  it('adds a custom model through the modal', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');
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
    await screen.findByLabelText('Enable Opus 4.8');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Edit Opus 4.8'));
    const dialog = await screen.findByRole('dialog');
    const nameInput = within(dialog).getByLabelText('Display name (optional)');
    await user.clear(nameInput);
    await user.type(nameInput, 'House Opus');
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(mockedApi.updateModel).toHaveBeenCalledWith(
        'eu.anthropic.claude-opus-4-8',
        expect.objectContaining({ display_name: 'House Opus' })
      )
    );
  });

  it('deletes a custom model after confirmation', async () => {
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');
    const user = userEvent.setup();

    await user.click(screen.getByLabelText('Delete Llama3 1 70B Instruct'));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() =>
      expect(mockedApi.deleteModel).toHaveBeenCalledWith('meta.llama3-1-70b-instruct-v1:0')
    );
  });

  it('hides the open-weight toggle when the provider is not Bedrock', async () => {
    mockedApi.getModelSettings.mockResolvedValue({
      ...structuredClone(SETTINGS),
      provider: 'anthropic',
    });
    render(<ModelSettingsPage />);
    await screen.findByLabelText('Enable Opus 4.8');

    expect(screen.queryByLabelText(/Include open-weight/)).not.toBeInTheDocument();
  });
});
