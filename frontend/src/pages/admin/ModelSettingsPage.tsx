import { useEffect, useState } from 'react';
import { Button, Card, CardContent, CardHeader, CardTitle, Select } from '@/components/ui';
import { getModelSettings, updateModelSettings, ModelSettings } from '@/services/adminApi';
import { useConfigStore } from '@/store/config';

export function ModelSettingsPage() {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [enabled, setEnabled] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getModelSettings()
      .then((data) => {
        setSettings(data);
        setEnabled(data.enabled_models);
        setDefaultModel(data.default_model);
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load model settings'))
      .finally(() => setIsLoading(false));
  }, []);

  const toggleModel = (model: string) => {
    setSaved(false);
    setEnabled((prev) =>
      prev.includes(model) ? prev.filter((m) => m !== model) : [...prev, model]
    );
  };

  // The default must be an enabled model; an empty selection cannot be saved.
  const defaultInvalid = !enabled.includes(defaultModel);
  const dirty =
    !!settings &&
    (defaultModel !== settings.default_model ||
      enabled.length !== settings.enabled_models.length ||
      enabled.some((m) => !settings.enabled_models.includes(m)));

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);
    setSaved(false);
    try {
      const data = await updateModelSettings({
        default_model: defaultModel,
        enabled_models: enabled,
      });
      setSettings(data);
      setEnabled(data.enabled_models);
      setDefaultModel(data.default_model);
      // Keep the chat-side config (header badge, session picker) in sync.
      useConfigStore.setState({
        config: {
          provider: data.provider,
          model: data.default_model,
          available_models: data.enabled_models.map((id) => {
            const known = data.available_models.find((m) => m.id === id);
            return { id, label: known?.label ?? id, legacy: known?.legacy ?? false };
          }),
        },
      });
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update model settings');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return <p className="text-gray-400">Loading…</p>;
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold">Model</h1>
        <p className="text-gray-400 mt-1">
          Check the models users may select for their conversations, and pick the default.
          The catalog is discovered from the configured AI provider at runtime.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-200 px-4 py-3 rounded-md">
          {error}
        </div>
      )}

      {settings && (
        <Card variant="bordered">
          <CardHeader>
            <CardTitle>Selectable Models</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1">
              {settings.available_models.map((model) => (
                <label
                  key={model.id}
                  className="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-700/50 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={enabled.includes(model.id)}
                    onChange={() => toggleModel(model.id)}
                    className="h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
                  />
                  <span className="text-sm text-gray-200">{model.label}</span>
                  <span className="text-xs text-gray-500 font-mono">{model.id}</span>
                  {model.legacy && (
                    <span className="text-[10px] uppercase tracking-wider text-amber-500/80 border border-amber-700/50 rounded px-1.5 py-0.5">
                      legacy
                    </span>
                  )}
                  {!model.priced && (
                    <span
                      className="text-[10px] uppercase tracking-wider text-gray-400 border border-gray-600 rounded px-1.5 py-0.5"
                      title="No pricing entry — cost tracking uses a conservative default estimate"
                    >
                      unpriced
                    </span>
                  )}
                  {model.id === settings.default_model && (
                    <span className="text-xs text-gray-500">(current default)</span>
                  )}
                </label>
              ))}
            </div>

            <Select
              label="Default model"
              // Catalog order, not toggle order — re-checking a model must not
              // shuffle the dropdown.
              options={settings.available_models
                .filter((m) => enabled.includes(m.id))
                .map((m) => ({ value: m.id, label: m.label }))}
              value={defaultModel}
              onChange={(e) => {
                setDefaultModel(e.target.value);
                setSaved(false);
              }}
              error={
                enabled.length === 0
                  ? 'Enable at least one model'
                  : defaultInvalid
                    ? 'Pick a default among the enabled models'
                    : undefined
              }
            />

            <div className="flex items-center gap-3">
              <Button
                onClick={handleSave}
                disabled={isSaving || !dirty || defaultInvalid || enabled.length === 0}
              >
                {isSaving ? 'Saving…' : 'Save'}
              </Button>
              {saved && <span className="text-sm text-green-400">Model settings updated</span>}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
