import { useState } from 'react';
import { Button, Input, Modal, Select } from '@/components/ui';
import { AddModelRequest, ModelInfo, UpdateModelRequest } from '@/services/adminApi';

// Per-vendor Bedrock tags + off-Bedrock providers. Only anthropic/bedrock route
// via the Anthropic Messages protocol; the rest go through Converse. The legacy
// bedrock-open tag is kept so pre-existing custom rows remain editable.
const PROVIDER_OPTIONS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'amazon', label: 'Amazon' },
  { value: 'meta', label: 'Meta' },
  { value: 'mistral', label: 'Mistral' },
  { value: 'cohere', label: 'Cohere' },
  { value: 'ai21', label: 'AI21' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'stability', label: 'Stability' },
  { value: 'claude-cli', label: 'Claude CLI' },
  { value: 'bedrock', label: 'Bedrock (Claude, legacy tag)' },
  { value: 'bedrock-open', label: 'Bedrock (other vendors, legacy tag)' },
];

interface Props {
  /** null = add a custom model; otherwise edit this one. */
  model: ModelInfo | null;
  defaultProvider: string;
  onClose: () => void;
  onSubmit: (
    payload: AddModelRequest | UpdateModelRequest,
    editing: ModelInfo | null
  ) => Promise<void>;
}

export function ModelFormModal({ model, defaultProvider, onClose, onSubmit }: Props) {
  const editing = model !== null;
  const [id, setId] = useState(model?.id ?? '');
  const [provider, setProvider] = useState(model?.provider ?? defaultProvider);
  const [displayName, setDisplayName] = useState(model?.label ?? '');
  const [endpoint, setEndpoint] = useState(model?.endpoint ?? '');
  const [arn, setArn] = useState(model?.arn ?? '');
  const [inputPrice, setInputPrice] = useState(
    model?.input_price != null ? String(model.input_price) : ''
  );
  const [outputPrice, setOutputPrice] = useState(
    model?.output_price != null ? String(model.output_price) : ''
  );
  const [legacy, setLegacy] = useState(model?.legacy ?? false);
  const [enabled, setEnabled] = useState(model?.enabled ?? false);
  const [optionsText, setOptionsText] = useState(
    model && Object.keys(model.options).length ? JSON.stringify(model.options, null, 2) : ''
  );
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    setError(null);

    if (!editing && !id.trim()) {
      setError('Model ID is required');
      return;
    }
    if ((inputPrice === '') !== (outputPrice === '')) {
      setError('Set both input and output price, or neither');
      return;
    }
    let options: Record<string, unknown> = {};
    if (optionsText.trim()) {
      try {
        options = JSON.parse(optionsText);
        if (typeof options !== 'object' || Array.isArray(options)) {
          throw new Error('not an object');
        }
      } catch {
        setError('Options must be a valid JSON object');
        return;
      }
    }

    const common = {
      display_name: displayName.trim() || null,
      endpoint: endpoint.trim() || null,
      arn: arn.trim() || null,
      options,
      input_price: inputPrice === '' ? null : Number(inputPrice),
      output_price: outputPrice === '' ? null : Number(outputPrice),
      legacy,
      enabled,
    };

    setSubmitting(true);
    try {
      const payload: AddModelRequest | UpdateModelRequest = editing
        ? common
        : { id: id.trim(), provider, ...common };
      await onSubmit(payload, model);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save model');
      setSubmitting(false);
    }
  };

  return (
    <Modal
      isOpen
      onClose={onClose}
      title={editing ? `Edit ${model.label}` : 'Add custom model'}
      size="lg"
      align="top"
    >
      <div className="space-y-4">
        {error && (
          <div className="bg-red-900/50 border border-red-700 text-red-200 px-3 py-2 rounded-md text-sm">
            {error}
          </div>
        )}

        {editing ? (
          <p className="text-xs text-gray-500 font-mono">{model.id}</p>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Model ID"
              placeholder="meta.llama3-1-70b-instruct-v1:0"
              value={id}
              onChange={(e) => setId(e.target.value)}
            />
            <Select
              label="Provider"
              options={PROVIDER_OPTIONS}
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            />
          </div>
        )}

        <Input
          label="Display name (optional)"
          placeholder="Derived from the ID when blank"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />

        <Input
          label="Endpoint (optional)"
          placeholder="Custom inference base URL"
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
        />

        <Input
          label="ARN (optional)"
          placeholder="arn:aws:bedrock:…:inference-profile/…"
          value={arn}
          onChange={(e) => setArn(e.target.value)}
        />

        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Input price ($/Mtok)"
            type="number"
            min="0"
            step="0.01"
            value={inputPrice}
            onChange={(e) => setInputPrice(e.target.value)}
          />
          <Input
            label="Output price ($/Mtok)"
            type="number"
            min="0"
            step="0.01"
            value={outputPrice}
            onChange={(e) => setOutputPrice(e.target.value)}
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">Options (JSON)</label>
          <textarea
            value={optionsText}
            onChange={(e) => setOptionsText(e.target.value)}
            rows={3}
            placeholder='{ "max_tokens": 8192 }'
            className="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div className="flex items-center gap-6">
          <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
            />
            Enabled
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
            <input
              type="checkbox"
              checked={legacy}
              onChange={(e) => setLegacy(e.target.checked)}
              className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
            />
            Legacy
          </label>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting}>
            {submitting ? 'Saving…' : editing ? 'Save changes' : 'Add model'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
