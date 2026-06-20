import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelFormModal } from './ModelFormModal';
import { ModelInfo } from '@/services/adminApi';

const EXISTING: ModelInfo = {
  id: 'meta.llama3-1-70b-instruct-v1:0',
  label: 'Llama 3.1 70B',
  provider: 'bedrock-open',
  enabled: true,
  is_default: false,
  source: 'custom',
  legacy: false,
  priced: true,
  endpoint: null,
  arn: 'arn:aws:bedrock:eu-west-1::model/x',
  options: { max_tokens: 4096 },
  capable_engines: ['inkwest-agent'],
  input_price: 0.72,
  output_price: 0.72,
};

describe('ModelFormModal', () => {
  it('requires a model ID in add mode', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelFormModal model={null} defaultProvider="bedrock" onClose={vi.fn()} onSubmit={onSubmit} />
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(screen.getByText('Model ID is required')).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('rejects a lone price', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelFormModal model={null} defaultProvider="bedrock" onClose={vi.fn()} onSubmit={onSubmit} />
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Model ID'), 'mistral.large-v1:0');
    await user.type(screen.getByLabelText('Input price ($/Mtok)'), '1.5');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(screen.getByText(/Set both input and output price/)).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('rejects malformed options JSON', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelFormModal model={null} defaultProvider="bedrock" onClose={vi.fn()} onSubmit={onSubmit} />
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Model ID'), 'mistral.large-v1:0');
    await user.type(screen.getByPlaceholderText(/max_tokens/), 'not json');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(screen.getByText('Options must be a valid JSON object')).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('submits a valid add payload with id + provider', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelFormModal model={null} defaultProvider="bedrock" onClose={vi.fn()} onSubmit={onSubmit} />
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Model ID'), 'mistral.large-v1:0');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const [payload, editing] = onSubmit.mock.calls[0];
    expect(editing).toBeNull();
    expect(payload).toMatchObject({ id: 'mistral.large-v1:0', provider: 'bedrock' });
  });

  it('submits an edit payload without id/provider and keeps editing target', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelFormModal
        model={EXISTING}
        defaultProvider="bedrock"
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />
    );
    const user = userEvent.setup();
    const name = screen.getByLabelText('Display name (optional)');
    await user.clear(name);
    await user.type(name, 'House Llama');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const [payload, editing] = onSubmit.mock.calls[0];
    expect(editing).toBe(EXISTING);
    expect(payload).not.toHaveProperty('id');
    expect(payload).not.toHaveProperty('provider');
    expect(payload).toMatchObject({ display_name: 'House Llama', input_price: 0.72 });
  });
});
