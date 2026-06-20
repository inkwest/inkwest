import { useEffect, useMemo, useState } from 'react';
import { Pencil, Plus, RefreshCw, Search, Star, Trash2 } from 'lucide-react';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  ConfirmDialog,
  Input,
} from '@/components/ui';
import { cn } from '@/lib/utils';
import {
  AddModelRequest,
  ModelInfo,
  ModelSettings,
  UpdateModelRequest,
  addModel,
  deleteModel,
  getModelSettings,
  refreshModels,
  updateModel,
  updateModelSettings,
} from '@/services/adminApi';
import { useConfigStore } from '@/store/config';
import { ModelFormModal } from './ModelFormModal';

// Group order in the list; unknown providers fall after the known ones.
// Anthropic first, then the other Bedrock vendors, then legacy/off-Bedrock tags.
const PROVIDER_ORDER = [
  'anthropic',
  'amazon',
  'meta',
  'mistral',
  'cohere',
  'ai21',
  'deepseek',
  'stability',
  'google',
  'nvidia',
  'qwen',
  'openai',
  'minimax',
  'twelvelabs',
  'writer',
  'luma',
  'zai',
  'bedrock',
  'bedrock-open',
  'claude-cli',
];
const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  amazon: 'Amazon',
  meta: 'Meta',
  mistral: 'Mistral',
  cohere: 'Cohere',
  ai21: 'AI21',
  deepseek: 'DeepSeek',
  stability: 'Stability',
  google: 'Google',
  nvidia: 'NVIDIA',
  qwen: 'Qwen',
  openai: 'OpenAI',
  minimax: 'MiniMax',
  twelvelabs: 'TwelveLabs',
  writer: 'Writer',
  luma: 'Luma',
  zai: 'Z.AI',
  bedrock: 'Bedrock (Claude)',
  'bedrock-open': 'Bedrock (other vendors)',
  'claude-cli': 'Claude CLI',
};

function providerRank(provider: string): number {
  const i = PROVIDER_ORDER.indexOf(provider);
  return i === -1 ? PROVIDER_ORDER.length : i;
}

// Highlight the (first) search-match substring so a filtered list shows *why* each row
// matched. Case-insensitive; returns the text unchanged when there's no query/match.
function highlightMatch(text: string, query: string) {
  const q = query.trim();
  const idx = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-amber-400/30 text-amber-100 rounded px-0.5">
        {text.slice(idx, idx + q.length)}
      </mark>
      {text.slice(idx + q.length)}
    </>
  );
}

function formatRefreshed(iso: string | null): string {
  if (!iso) return 'never';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? 'never' : d.toLocaleString();
}

export function ModelSettingsPage() {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  // Staged enable/default selection; metadata edits apply immediately.
  const [enabled, setEnabled] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState('');
  const [search, setSearch] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // null = closed; { model: null } = add; { model } = edit.
  const [formState, setFormState] = useState<{ model: ModelInfo | null } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ModelInfo | null>(null);

  // Adopt a fresh settings payload (initial load + every write that returns one).
  // Immediate ops (add/edit/delete/refresh) are authoritative server state and
  // intentionally reset the staged enable/default selection to it; the user
  // re-applies any in-progress checkbox changes (which require a Save anyway).
  const adopt = (data: ModelSettings) => {
    setSettings(data);
    setEnabled(data.enabled_models);
    setDefaultModel(data.default_model);
  };

  useEffect(() => {
    getModelSettings()
      .then(adopt)
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load model settings'))
      .finally(() => setIsLoading(false));
  }, []);

  // Keep the chat-side config (header badge, session picker) in sync after a save.
  const syncConfig = (data: ModelSettings) => {
    useConfigStore.setState((s) => ({
      config: {
        provider: data.provider,
        model: data.default_model,
        available_models: data.models
          .filter((m) => data.enabled_models.includes(m.id))
          .map((m) => ({ id: m.id, label: m.label, legacy: m.legacy })),
        // Preserve attachments_enabled (sandbox/provider flag, unrelated to model settings).
        attachments_enabled: s.config?.attachments_enabled ?? false,
      },
    }));
  };

  // Persist a complete selection immediately (save-on-click — there is no Save
  // form). An empty selection is refused client-side: the backend requires at
  // least one enabled model and a default within the enabled set.
  const persist = async (nextDefault: string, nextEnabled: string[]) => {
    setEnabled(nextEnabled);
    setDefaultModel(nextDefault);
    if (nextEnabled.length === 0) {
      setError('Enable at least one model.');
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      const data = await updateModelSettings({
        default_model: nextDefault,
        enabled_models: nextEnabled,
      });
      adopt(data);
      syncConfig(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update model settings');
    } finally {
      setIsSaving(false);
    }
  };

  // Toggle a model's enabled state and save. Disabling the current default
  // auto-reassigns it to the first remaining enabled model so the selection
  // stays valid without a manual step.
  const toggleEnabled = (id: string) => {
    const next = enabled.includes(id) ? enabled.filter((m) => m !== id) : [...enabled, id];
    const nextDefault = next.includes(defaultModel) ? defaultModel : (next[0] ?? '');
    void persist(nextDefault, next);
  };

  // Star a model as the default and save. Only enabled models are eligible (the
  // row gates the call), so the resulting default is always within the set.
  const setDefault = (id: string) => void persist(id, enabled);

  const handleRefresh = async (includeOpen?: boolean) => {
    setIsRefreshing(true);
    setError(null);
    try {
      adopt(await refreshModels(includeOpen));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to refresh catalog');
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const model = deleteTarget;
    setDeleteTarget(null);
    setError(null);
    try {
      adopt(await deleteModel(model.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete model');
    }
  };

  const handleFormSubmit = async (
    payload: AddModelRequest | UpdateModelRequest,
    editing: ModelInfo | null
  ) => {
    const data = editing
      ? await updateModel(editing.id, payload as UpdateModelRequest)
      : await addModel(payload as AddModelRequest);
    adopt(data);
    setFormState(null);
  };

  const grouped = useMemo(() => {
    if (!settings) return [];
    const q = search.trim().toLowerCase();
    const matches = settings.models.filter(
      (m) => !q || m.id.toLowerCase().includes(q) || m.label.toLowerCase().includes(q)
    );
    const byProvider = new Map<string, ModelInfo[]>();
    for (const m of matches) {
      const list = byProvider.get(m.provider) ?? [];
      list.push(m);
      byProvider.set(m.provider, list);
    }
    return [...byProvider.entries()].sort(
      ([a], [b]) => providerRank(a) - providerRank(b) || a.localeCompare(b)
    );
  }, [settings, search]);

  if (isLoading) {
    return <p className="text-gray-400">Loading…</p>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">
          Model
          {isSaving && <span className="ml-3 align-middle text-sm font-normal text-gray-400">Saving…</span>}
        </h1>
        <p className="text-gray-400 mt-1">
          Enable the models users may select, set the default, and manage per-model metadata
          and pricing. The catalog is discovered from the configured AI provider at runtime.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-200 px-4 py-3 rounded-md">
          {error}
        </div>
      )}

      {settings && (
        <>
          {/* Catalog status + controls */}
          <Card variant="bordered">
            <CardContent className="space-y-4 pt-6">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="text-sm text-gray-400">
                  Provider <span className="text-gray-200 font-medium">{settings.provider}</span>
                  {settings.inkwest_agent_enabled && (
                    <span className="text-gray-200 font-medium"> + inkwest-agent engine</span>
                  )}{' '}
                  ·{' '}
                  {settings.catalog.count} models ·{' '}
                  <span className={settings.catalog.source === 'live' ? 'text-green-400' : 'text-amber-400'}>
                    {settings.catalog.source}
                  </span>{' '}
                  · refreshed {formatRefreshed(settings.catalog.last_refreshed_at)}
                  {settings.catalog.last_fetch_failed && (
                    <span className="text-red-400"> · last fetch failed</span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="secondary" onClick={() => setFormState({ model: null })}>
                    <Plus className="w-4 h-4 mr-1" /> Add model
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => handleRefresh()}
                    disabled={isRefreshing}
                  >
                    <RefreshCw className={`w-4 h-4 mr-1 ${isRefreshing ? 'animate-spin' : ''}`} />
                    {isRefreshing ? 'Refreshing…' : 'Refresh from provider'}
                  </Button>
                </div>
              </div>

              {/* Show this toggle only when it's actually useful — i.e. the
                  inkwest-agent engine (custom Converse loop) is enabled and can
                  run non-Anthropic Bedrock models. Under claude-cli only, the CLI
                  can't drive them, so the toggle is hidden (catalog stays Claude-only). */}
              {settings.inkwest_agent_enabled && (
                <label className="flex items-center gap-3 text-sm text-gray-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={settings.include_all_vendors}
                    disabled={isRefreshing}
                    onChange={(e) => handleRefresh(e.target.checked)}
                    className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
                  />
                  Include all Bedrock vendors (Amazon, Meta, Mistral, Cohere…). Toggling refreshes
                  the catalog.
                </label>
              )}

              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
                <Input
                  placeholder="Quick search by name or ID… (Esc to clear)"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onKeyDown={(e) => e.key === 'Escape' && setSearch('')}
                  className="pl-9"
                />
              </div>
            </CardContent>
          </Card>

          {/* Grouped model list — vendor cards auto-flow into 2 balanced columns
              (CSS multi-column masonry) so short groups (e.g. a single Cohere model)
              don't each eat a full-width row. break-inside-avoid keeps a card whole. */}
          <div className="columns-1 md:columns-2 gap-6 [&>*]:mb-6 [&>*]:break-inside-avoid">
          {grouped.map(([provider, models]) => (
            <Card key={provider} variant="bordered">
              <CardHeader>
                <CardTitle>
                  {PROVIDER_LABELS[provider] ??
                    provider.charAt(0).toUpperCase() + provider.slice(1)}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-1">
                {models.map((model) => {
                  const isEnabled = enabled.includes(model.id);
                  const isDefault = defaultModel === model.id;
                  return (
                  <div
                    key={model.id}
                    className="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-700/50"
                  >
                    <input
                      type="checkbox"
                      checked={isEnabled}
                      disabled={isSaving}
                      onChange={() => toggleEnabled(model.id)}
                      aria-label={`Enable ${model.label}`}
                      className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500 disabled:opacity-50"
                    />
                    {/* Star = default. Settable only when enabled (backend rejects a
                        default outside the enabled set); disabled+dimmed otherwise. */}
                    <button
                      type="button"
                      onClick={() => setDefault(model.id)}
                      disabled={!isEnabled || isSaving}
                      aria-label={isDefault ? `${model.label} is the default` : `Set ${model.label} as default`}
                      aria-pressed={isDefault}
                      title={
                        !isEnabled
                          ? 'Enable this model to set it as default'
                          : isDefault
                            ? 'Default model'
                            : 'Set as default'
                      }
                      className={cn(
                        'p-1 rounded focus:outline-none focus:ring-2 focus:ring-blue-500',
                        isDefault
                          ? 'text-amber-400'
                          : isEnabled
                            ? 'text-gray-500 hover:text-amber-300'
                            : 'text-gray-700 cursor-not-allowed',
                      )}
                    >
                      <Star className="w-4 h-4" fill={isDefault ? 'currentColor' : 'none'} />
                    </button>
                    <span className="text-sm text-gray-200 whitespace-nowrap shrink-0">
                      {highlightMatch(model.label, search)}
                    </span>
                    <span className="text-xs text-gray-500 font-mono truncate flex-1 min-w-0">
                      {highlightMatch(model.id, search)}
                    </span>
                    {model.source === 'custom' && <Badge variant="info">custom</Badge>}
                    {model.legacy && <Badge variant="warning">legacy</Badge>}
                    {model.priced ? (
                      <span className="text-xs text-gray-500">
                        ${model.input_price}/${model.output_price} per Mtok
                      </span>
                    ) : (
                      <Badge variant="danger" title="No pricing — cost tracking uses a default estimate">
                        unpriced
                      </Badge>
                    )}
                    <div className="ml-auto flex items-center gap-1">
                      <button
                        onClick={() => setFormState({ model })}
                        aria-label={`Edit ${model.label}`}
                        className="p-1.5 text-gray-400 hover:text-gray-200 rounded"
                      >
                        <Pencil className="w-4 h-4" />
                      </button>
                      {model.source === 'custom' && (
                        <button
                          onClick={() => setDeleteTarget(model)}
                          aria-label={`Delete ${model.label}`}
                          className="p-1.5 text-gray-400 hover:text-red-400 rounded"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </div>
                  );
                })}
              </CardContent>
            </Card>
          ))}
          </div>

          {grouped.length === 0 && (
            <p className="text-gray-500 text-sm">No models match “{search}”.</p>
          )}
        </>
      )}

      {formState && (
        <ModelFormModal
          model={formState.model}
          defaultProvider={settings?.provider ?? 'anthropic'}
          onClose={() => setFormState(null)}
          onSubmit={handleFormSubmit}
        />
      )}

      <ConfirmDialog
        isOpen={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
        title="Delete custom model"
        message={
          deleteTarget
            ? `Delete custom model "${deleteTarget.label}" (${deleteTarget.id})? This cannot be undone.`
            : ''
        }
        confirmLabel="Delete"
        variant="danger"
      />
    </div>
  );
}
