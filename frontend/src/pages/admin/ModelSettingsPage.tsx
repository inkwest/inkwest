import { useEffect, useState, type Dispatch, type SetStateAction } from 'react';
import {
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Star,
  Trash2,
} from 'lucide-react';
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
import { engineLabel } from '@/lib/engines';
import {
  hasModality,
  INPUT_MODALITIES,
  MODALITY_META,
  TEXT_META,
  type InputModality,
} from '@/lib/modality';
import { ModalityIcons } from '@/components/ModalityIcons';
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

// Group order within an engine section; unknown providers fall after the known ones.
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

const providerLabel = (provider: string) =>
  PROVIDER_LABELS[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1);

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

// Group models by provider, ordered by providerRank then id.
function groupByProvider(models: ModelInfo[]): [string, ModelInfo[]][] {
  const byProvider = new Map<string, ModelInfo[]>();
  for (const m of models) {
    const list = byProvider.get(m.provider) ?? [];
    list.push(m);
    byProvider.set(m.provider, list);
  }
  return [...byProvider.entries()].sort(
    ([a], [b]) => providerRank(a) - providerRank(b) || a.localeCompare(b)
  );
}

// The engine availability a model is offered on, from the registry: an explicit
// options.engines list when curated, else all engines the model is capable of.
function modelAvailability(m: ModelInfo): string[] {
  if (!m.enabled) return [];
  const opt = m.options?.engines;
  return Array.isArray(opt) ? (opt as string[]) : m.capable_engines;
}

// Input filter facet: the badged modalities (shared module) plus an explicit TEXT
// category for text-only models. Listing TEXT makes the input filter a complete
// partition — every model belongs to at least one category — so unchecking all of them
// hides everything, symmetric with the output facet. (Per-model badges omit TEXT.)
type InputFacet = 'TEXT' | InputModality;
const INPUT_FACETS: InputFacet[] = ['TEXT', ...INPUT_MODALITIES];

// Output facet: a model is either chat-capable (TEXT output) or not (embedding /
// image-gen / video output). Both are filterable as pre-checked "show" toggles.
type OutputKind = 'CHAT' | 'NONCHAT';
const OUTPUT_KINDS: OutputKind[] = ['CHAT', 'NONCHAT'];

// Shared styling for every filter checkbox, so the control set stays visually coherent.
const FILTER_CHECKBOX = 'h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500';

// Non-TEXT output (embedding / image-gen / video) ⇒ not a chat model: it can't drive a
// conversation, so we badge it as non-chat and the output filter can hide it.
function isChatCapable(m: ModelInfo): boolean {
  const out = m.output_modalities ?? [];
  // Empty (unknown) is treated as chat-capable (back-compat: a model with no live modality
  // data — e.g. a static-catalog Claude row — is a normal chat model).
  return out.length === 0 || out.some((x) => x.toUpperCase() === 'TEXT');
}

export function ModelSettingsPage() {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  // Staged per-(engine, model) offering: model_id -> engine ids it's exposed on.
  // A model is "enabled" iff it's offered on at least one engine.
  const [availability, setAvailability] = useState<Record<string, string[]>>({});
  // The default is an (engine, model) pair — a model can run on more than one engine.
  const [defaultModel, setDefaultModel] = useState('');
  const [defaultEngine, setDefaultEngine] = useState('');
  const [search, setSearch] = useState('');
  // Capability filter (P1.8): two facets, both as pre-checked "show" toggles (all on =
  // nothing hidden). Input = the allowed input categories (TEXT + badged modalities);
  // a model is kept while at least one of its categories is checked. Output = the
  // allowed output kinds; a model is kept while its kind (chat / non-chat) is allowed.
  const [allowedInputs, setAllowedInputs] = useState<Set<InputFacet>>(
    () => new Set(INPUT_FACETS)
  );
  const [allowedOutputs, setAllowedOutputs] = useState<Set<OutputKind>>(
    () => new Set(OUTPUT_KINDS)
  );
  // Toggle one value in a Set-valued filter (immutable update).
  const toggleIn = <T,>(setState: Dispatch<SetStateAction<Set<T>>>, value: T) =>
    setState((prev) => {
      const next = new Set(prev);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return next;
    });
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // null = closed; { model: null } = add; { model } = edit.
  const [formState, setFormState] = useState<{ model: ModelInfo | null } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ModelInfo | null>(null);

  // Adopt a fresh settings payload (initial load + every write that returns one).
  // Rebuilds the staged per-engine availability from the authoritative server state.
  const adopt = (data: ModelSettings) => {
    setSettings(data);
    setDefaultModel(data.default_model);
    setDefaultEngine(data.default_engine);
    const avail: Record<string, string[]> = {};
    for (const m of data.models) avail[m.id] = modelAvailability(m);
    setAvailability(avail);
  };

  useEffect(() => {
    getModelSettings()
      .then(adopt)
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load model settings'))
      .finally(() => setIsLoading(false));
  }, []);

  // Keep the chat-side config (session picker) in sync after a save by re-fetching the
  // authoritative app config — it computes the enabled set + per-model runnable engines
  // server-side, so we don't duplicate that logic here.
  const syncConfig = () => {
    void useConfigStore.getState().refreshConfig();
  };

  const enabledIds = (avail: Record<string, string[]>) =>
    Object.keys(avail).filter((id) => (avail[id]?.length ?? 0) > 0);

  // Persist a complete selection immediately (save-on-click — there is no Save form).
  // The default is an (engine, model) pair; it's reconciled against the offering so it
  // always names an enabled model on an engine it's actually offered on. An empty
  // selection is refused client-side (the backend requires ≥1 enabled model).
  const persist = async (
    wantModel: string,
    wantEngine: string,
    nextAvail: Record<string, string[]>
  ) => {
    setAvailability(nextAvail);
    const enabled = enabledIds(nextAvail);
    if (enabled.length === 0) {
      setDefaultModel(wantModel);
      setDefaultEngine(wantEngine);
      setError('Enable at least one model on an engine.');
      return;
    }
    // Keep the default valid: fall back to the first enabled model, and to that model's
    // first offered engine, when the requested pairing is no longer offered.
    const dm = enabled.includes(wantModel) ? wantModel : enabled[0];
    const dmEngines = nextAvail[dm] ?? [];
    const de = dmEngines.includes(wantEngine) ? wantEngine : (dmEngines[0] ?? '');
    setDefaultModel(dm);
    setDefaultEngine(de);
    setIsSaving(true);
    setError(null);
    try {
      const engine_availability: Record<string, string[]> = {};
      for (const id of enabled) engine_availability[id] = nextAvail[id];
      const data = await updateModelSettings({
        default_model: dm,
        default_engine: de,
        enabled_models: enabled,
        engine_availability,
      });
      adopt(data);
      syncConfig();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update model settings');
    } finally {
      setIsSaving(false);
    }
  };

  // Toggle a single (engine, model) offering and save. Removing the default pairing's
  // engine reassigns the default (handled in persist).
  const toggleEngineModel = (engine: string, id: string) => {
    const cur = availability[id] ?? [];
    const nextEngines = cur.includes(engine) ? cur.filter((e) => e !== engine) : [...cur, engine];
    void persist(defaultModel, defaultEngine, { ...availability, [id]: nextEngines });
  };

  // Enable/disable an engine across a set of models (a whole engine section, or one
  // provider sub-block within it). Only touches models capable of that engine.
  const setEngineForModels = (engine: string, models: ModelInfo[], on: boolean) => {
    const next = { ...availability };
    for (const m of models) {
      if (!m.capable_engines.includes(engine)) continue;
      const cur = next[m.id] ?? [];
      if (on) next[m.id] = cur.includes(engine) ? cur : [...cur, engine];
      else next[m.id] = cur.filter((e) => e !== engine);
    }
    void persist(defaultModel, defaultEngine, next);
  };

  // Star an (engine, model) pairing as the default and save. Only a pairing the model is
  // actually offered on is eligible (the row gates the call).
  const setDefault = (engine: string, id: string) => void persist(id, engine, availability);

  const handleRefresh = async (includeOpen?: boolean) => {
    setIsRefreshing(true);
    setError(null);
    try {
      const next = await refreshModels(includeOpen);
      adopt(next);
      // Discovery degrades to the static catalog on failure and still returns 200 — surface
      // the reason loudly instead of letting the refresh look like it silently did nothing.
      if (next.catalog.last_fetch_failed) {
        setError(
          `Live discovery failed: ${next.catalog.error ?? 'unknown error'} — showing the cached/static catalog.`
        );
      }
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

  const toggleCollapsed = (engine: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(engine)) next.delete(engine);
      else next.add(engine);
      return next;
    });

  if (isLoading) {
    return <p className="text-gray-400">Loading…</p>;
  }

  const q = search.trim().toLowerCase();
  const matchesQuery = (m: ModelInfo) => {
    if (q && !(m.id.toLowerCase().includes(q) || m.label.toLowerCase().includes(q))) return false;
    // Input facet: a model belongs to TEXT when it accepts text (declared, or empty/
    // unknown = text-only) AND to each badged modality it declares — same rule as the
    // per-model icons, so a TEXT+IMAGE model lives in BOTH facets and stays visible when
    // either is checked. It passes while at least one of its categories is checked; a
    // model with no recognised category isn't hidden (keeps "all on = nothing hidden").
    const badged = INPUT_MODALITIES.filter((mod) => hasModality(m.input_modalities, mod));
    const acceptsText = hasModality(m.input_modalities, 'TEXT') || !m.input_modalities?.length;
    const inputCats: InputFacet[] = [...(acceptsText ? (['TEXT'] as InputFacet[]) : []), ...badged];
    if (inputCats.length > 0 && !inputCats.some((cat) => allowedInputs.has(cat))) return false;
    // Output facet: keep the model while its kind (chat / non-chat) is allowed.
    if (!allowedOutputs.has(isChatCapable(m) ? 'CHAT' : 'NONCHAT')) return false;
    return true;
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">
          Model
          {isSaving && <span className="ml-3 align-middle text-sm font-normal text-gray-400">Saving…</span>}
        </h1>
        <p className="text-gray-400 mt-1">
          Pick which models each engine offers, set the default, and manage per-model metadata
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

              {/* Capability filter (P1.8): two facets of pre-checked "show" toggles —
                  input modality and output kind. All on = nothing hidden. */}
              <div className="space-y-2 text-sm text-gray-300">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="w-16 shrink-0 text-gray-400">Input</span>
                  {INPUT_FACETS.map((facet) => {
                    const Icon = facet === 'TEXT' ? TEXT_META.Icon : MODALITY_META[facet].Icon;
                    const label = facet === 'TEXT' ? 'Text' : facet;
                    return (
                      <label key={facet} className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={allowedInputs.has(facet)}
                          onChange={() => toggleIn(setAllowedInputs, facet)}
                          aria-label={`Show ${facet === 'TEXT' ? 'text-only' : facet} input models`}
                          className={FILTER_CHECKBOX}
                        />
                        <Icon className="w-3.5 h-3.5 text-gray-400" />
                        {label}
                      </label>
                    );
                  })}
                </div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="w-16 shrink-0 text-gray-400">Output</span>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allowedOutputs.has('CHAT')}
                      onChange={() => toggleIn<OutputKind>(setAllowedOutputs, 'CHAT')}
                      aria-label="Show chat models"
                      className={FILTER_CHECKBOX}
                    />
                    Chat
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allowedOutputs.has('NONCHAT')}
                      onChange={() => toggleIn<OutputKind>(setAllowedOutputs, 'NONCHAT')}
                      aria-label="Show non-chat models"
                      className={FILTER_CHECKBOX}
                    />
                    Non-chat
                    <span className="text-gray-500">(embedding / image-gen)</span>
                  </label>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* One collapsible section per enabled engine. Within a section, models the
              engine can run are grouped by provider; the checkbox toggles whether that
              model is OFFERED on this engine (a Claude model appears in every section). */}
          {settings.engines.map((engine) => {
            const sectionModels = settings.models.filter(
              (m) => m.capable_engines.includes(engine) && matchesQuery(m)
            );
            const grouped = groupByProvider(sectionModels);
            const isOn = (m: ModelInfo) => (availability[m.id] ?? []).includes(engine);
            const allOn = sectionModels.length > 0 && sectionModels.every(isOn);
            const noneOn = sectionModels.every((m) => !isOn(m));
            const offered = sectionModels.filter(isOn).length;
            const isCollapsed = collapsed.has(engine);
            return (
              <Card key={engine} variant="bordered">
                <CardHeader>
                  <div className="flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() => toggleCollapsed(engine)}
                      aria-expanded={!isCollapsed}
                      className="flex items-center gap-2 text-left"
                    >
                      {isCollapsed ? (
                        <ChevronRight className="w-4 h-4 text-gray-400" />
                      ) : (
                        <ChevronDown className="w-4 h-4 text-gray-400" />
                      )}
                      <CardTitle>{engineLabel(engine)}</CardTitle>
                      <span className="text-xs text-gray-500">
                        {offered}/{sectionModels.length} offered
                      </span>
                    </button>
                    {/* Bulk enable/disable every (visible) model on this engine. */}
                    <div className="flex items-center gap-1 text-xs">
                      <button
                        type="button"
                        onClick={() => setEngineForModels(engine, sectionModels, true)}
                        disabled={isSaving || allOn || sectionModels.length === 0}
                        aria-label={`Offer all on ${engineLabel(engine)}`}
                        className="px-1.5 py-0.5 rounded text-gray-400 hover:text-gray-200 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        All
                      </button>
                      <span className="text-gray-600">·</span>
                      <button
                        type="button"
                        onClick={() => setEngineForModels(engine, sectionModels, false)}
                        disabled={isSaving || noneOn}
                        aria-label={`Offer none on ${engineLabel(engine)}`}
                        className="px-1.5 py-0.5 rounded text-gray-400 hover:text-gray-200 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        None
                      </button>
                    </div>
                  </div>
                </CardHeader>
                {!isCollapsed && (
                  <CardContent className="space-y-4">
                    {grouped.length === 0 && (
                      <p className="text-gray-500 text-sm">No models match “{search}”.</p>
                    )}
                    {grouped.map(([provider, models]) => {
                      const provAllOn = models.every(isOn);
                      const provNoneOn = models.every((m) => !isOn(m));
                      return (
                        <div key={provider} className="space-y-1">
                          <div className="flex items-center justify-between gap-2 px-1">
                            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                              {providerLabel(provider)}
                            </h3>
                            <div className="flex items-center gap-1 text-xs">
                              <button
                                type="button"
                                onClick={() => setEngineForModels(engine, models, true)}
                                disabled={isSaving || provAllOn}
                                aria-label={`Offer all ${providerLabel(provider)} on ${engineLabel(engine)}`}
                                className="px-1.5 py-0.5 rounded text-gray-500 hover:text-gray-200 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                All
                              </button>
                              <span className="text-gray-600">·</span>
                              <button
                                type="button"
                                onClick={() => setEngineForModels(engine, models, false)}
                                disabled={isSaving || provNoneOn}
                                aria-label={`Offer no ${providerLabel(provider)} on ${engineLabel(engine)}`}
                                className="px-1.5 py-0.5 rounded text-gray-500 hover:text-gray-200 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                None
                              </button>
                            </div>
                          </div>
                          {models.map((model) => {
                            const offeredHere = isOn(model);
                            // The default is an (engine, model) pair: this row is the default
                            // only in the section whose engine matches. Settable only where the
                            // model is offered, so picking the star also picks the engine.
                            const isDefault = defaultModel === model.id && defaultEngine === engine;
                            const eligibleDefault = offeredHere;
                            return (
                              <div
                                key={model.id}
                                className="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-700/50"
                              >
                                <input
                                  type="checkbox"
                                  checked={offeredHere}
                                  disabled={isSaving}
                                  onChange={() => toggleEngineModel(engine, model.id)}
                                  aria-label={`Offer ${model.label} on ${engineLabel(engine)}`}
                                  className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500 disabled:opacity-50"
                                />
                                {/* Star = default (model-level). Settable only when the model
                                    is offered on some engine (backend rejects a default outside
                                    the enabled set); disabled+dimmed otherwise. */}
                                <button
                                  type="button"
                                  onClick={() => setDefault(engine, model.id)}
                                  disabled={!eligibleDefault || isSaving}
                                  aria-label={
                                    isDefault
                                      ? `${model.label} on ${engineLabel(engine)} is the default`
                                      : `Set ${model.label} on ${engineLabel(engine)} as default`
                                  }
                                  aria-pressed={isDefault}
                                  title={
                                    !eligibleDefault
                                      ? 'Offer this model on this engine to make it the default'
                                      : isDefault
                                        ? 'Default Engine·Model'
                                        : 'Set as default'
                                  }
                                  className={cn(
                                    'p-1 rounded focus:outline-none focus:ring-2 focus:ring-blue-500',
                                    isDefault
                                      ? 'text-amber-400'
                                      : eligibleDefault
                                        ? 'text-gray-500 hover:text-amber-300'
                                        : 'text-gray-700 cursor-not-allowed'
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
                                <ModalityIcons modalities={model.input_modalities} />
                                {model.source === 'custom' && <Badge variant="info">custom</Badge>}
                                {model.legacy && <Badge variant="warning">legacy</Badge>}
                                {/* Non-chat output (embedding / image-gen / video) — visually
                                    distinct so an admin doesn't enable it for conversations. */}
                                {!isChatCapable(model) && (
                                  <Badge
                                    variant="warning"
                                    title={`Non-chat output: ${(model.output_modalities ?? []).join(', ')}`}
                                  >
                                    non-chat
                                  </Badge>
                                )}
                                {model.priced ? (
                                  <span className="text-xs text-gray-500">
                                    ${model.input_price}/${model.output_price} per Mtok
                                  </span>
                                ) : (
                                  <Badge
                                    variant="danger"
                                    title="No pricing — cost tracking uses a default estimate"
                                  >
                                    unpriced
                                  </Badge>
                                )}
                                <div className="ml-auto flex items-center gap-1">
                                  <button
                                    onClick={() => setFormState({ model })}
                                    aria-label={`Edit ${model.label} (${engineLabel(engine)} section)`}
                                    className="p-1.5 text-gray-400 hover:text-gray-200 rounded"
                                  >
                                    <Pencil className="w-4 h-4" />
                                  </button>
                                  {model.source === 'custom' && (
                                    <button
                                      onClick={() => setDeleteTarget(model)}
                                      aria-label={`Delete ${model.label} (${engineLabel(engine)} section)`}
                                      className="p-1.5 text-gray-400 hover:text-red-400 rounded"
                                    >
                                      <Trash2 className="w-4 h-4" />
                                    </button>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      );
                    })}
                  </CardContent>
                )}
              </Card>
            );
          })}
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
