import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Download, Upload, GitFork } from 'lucide-react';
import { Button, Modal } from '@/components/ui';
import { SkillsManager } from '@/components/skills/SkillsManager';
import { McpPolicySettingsTab } from '@/components/settings/McpPolicySettingsTab';
import { usePreferencesStore } from '@/store/preferences';
import { useSettingsStore } from '@/store/settings';
import { getSkills } from '@/services/api';
import type { ConfigExport, Skill } from '@/types/api';

type Tab = 'display' | 'skills' | 'mcp' | 'io';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  // Tab to show when the modal opens (e.g. the MCP panel deep-links to 'mcp').
  initialTab?: Tab;
}

export function SettingsModal({ isOpen, onClose, initialTab = 'display' }: SettingsModalProps) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [adminSkills, setAdminSkills] = useState<Skill[]>([]);
  const prefs = usePreferencesStore();

  // Load DB-backed prefs + the admin skill list when the modal first opens; also select the
  // requested tab (a deep-link may open straight onto 'mcp').
  useEffect(() => {
    if (!isOpen) return;
    setTab(initialTab);
    if (!prefs.loaded) void prefs.load();
    void getSkills().then(setAdminSkills).catch(() => setAdminSkills([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, initialTab]);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Settings"
      size="xl"
      align="top"
      // MCP tab manages its own two-pane scroll; pin tabs + let the panes be the
      // buffer so the tab content never pushes past the viewport. Other tabs keep
      // the default whole-body scroll.
      bodyClassName={tab === 'mcp' ? 'flex min-h-0 flex-col overflow-hidden' : undefined}
    >
      <div className="flex shrink-0 gap-2 border-b border-gray-700 -mt-2 mb-4">
        {([
          ['display', 'Display'],
          ['skills', 'Skills'],
          ['mcp', 'MCP tools'],
          ['io', 'Import / Export'],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-3 py-2 text-sm border-b-2 -mb-px transition-colors ${
              tab === key
                ? 'border-blue-500 text-white'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Gate writes on a completed load: editing before the server document arrives would
          PUT a document missing skills.disabled_admin and silently drop the user's choices. */}
      {!prefs.loaded ? (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
        </div>
      ) : (
        <>
          {tab === 'display' && <DisplayTab />}
          {tab === 'skills' && <SkillsTab adminSkills={adminSkills} />}
          {tab === 'mcp' && <McpPolicySettingsTab />}
          {tab === 'io' && <ImportExportTab />}
        </>
      )}
    </Modal>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className="w-full flex items-center justify-between px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
    >
      <span>{label}</span>
      {checked && <Check className="h-4 w-4 text-green-400" />}
    </button>
  );
}

function DisplayTab() {
  const { compactToolCards, showTimestamps, wideComposer } = useSettingsStore();
  const saveUi = usePreferencesStore((s) => s.saveUi);
  const error = usePreferencesStore((s) => s.error);
  return (
    <div className="space-y-1">
      <ToggleRow
        label="Compact tool cards"
        checked={compactToolCards}
        onChange={(v) => void saveUi({ compactToolCards: v })}
      />
      <ToggleRow
        label="Show timestamps"
        checked={showTimestamps}
        onChange={(v) => void saveUi({ showTimestamps: v })}
      />
      <ToggleRow
        label="Wide composer"
        checked={wideComposer}
        onChange={(v) => void saveUi({ wideComposer: v })}
      />
      {error && <p className="px-3 pt-2 text-xs text-red-400">{error}</p>}
      <p className="px-3 pt-3 text-xs text-gray-500">
        Saved to your account and synced across devices.
      </p>
    </div>
  );
}

function SkillsTab({ adminSkills }: { adminSkills: Skill[] }) {
  const prefs = usePreferencesStore();
  const personalNames = useMemo(() => new Set(prefs.skills.map((s) => s.name)), [prefs.skills]);
  const disabled = useMemo(() => new Set(prefs.disabledAdmin), [prefs.disabledAdmin]);

  const overridable = adminSkills.map((a) => ({
    name: a.name,
    alreadyOverridden: personalNames.has(a.name),
  }));

  return (
    <div className="space-y-6">
      <section>
        <h4 className="text-sm font-semibold text-gray-200 mb-2">Your personal skills</h4>
        <SkillsManager
          skills={prefs.skills}
          error={prefs.error}
          clearError={prefs.clearError}
          onCreate={prefs.createSkill}
          onUpdate={prefs.updateSkill}
          onDelete={prefs.deleteSkill}
          onOverride={prefs.overrideSkill}
          overridableAdmin={overridable}
          showOverrideBadge
          emptyHint="No personal skills yet. Add one, or override a global skill above."
        />
      </section>

      {adminSkills.length > 0 && (
        <section>
          <h4 className="text-sm font-semibold text-gray-200 mb-2">Global skills</h4>
          <p className="text-xs text-gray-500 mb-2">
            Turn a global skill off for yourself, or override it (above) to edit your own copy.
            A personal skill of the same name always wins.
          </p>
          <div className="space-y-1">
            {adminSkills.map((a) => {
              const shadowed = personalNames.has(a.name);
              const enabled = !disabled.has(a.name);
              return (
                <div
                  key={a.id}
                  className="flex items-center justify-between px-3 py-2 rounded-md hover:bg-gray-700/50"
                >
                  <div className="min-w-0">
                    <span className="text-sm text-gray-200">{a.name}</span>
                    {shadowed && (
                      <span className="ml-2 inline-flex items-center gap-1 text-xs text-yellow-400">
                        <GitFork className="w-3 h-3" /> overridden
                      </span>
                    )}
                  </div>
                  <button
                    disabled={shadowed}
                    onClick={() => void prefs.setAdminSkillEnabled(a.name, !enabled)}
                    className={`text-xs px-2 py-1 rounded ${
                      shadowed
                        ? 'bg-gray-700 text-gray-500 cursor-not-allowed'
                        : enabled
                          ? 'bg-green-900/50 text-green-400'
                          : 'bg-red-900/50 text-red-400'
                    }`}
                    title={shadowed ? 'Shadowed by your personal skill' : undefined}
                  >
                    {enabled ? 'Enabled' : 'Disabled'}
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

function ImportExportTab() {
  const { exportConfig, importConfig, error } = usePreferencesStore();
  const fileRef = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<ConfigExport | null>(null);
  const [replaceAll, setReplaceAll] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handleExport = async () => {
    setDone(null);
    const cfg = await exportConfig();
    const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `inkwest-config-${cfg.exported_at.slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleFile = async (file: File) => {
    setParseError(null);
    setDone(null);
    try {
      const parsed = JSON.parse(await file.text()) as ConfigExport;
      const validShape =
        parsed != null &&
        typeof parsed.schema_version === 'number' &&
        Array.isArray(parsed.skills) &&
        parsed.skills.every((s) => s != null && typeof s.content === 'string');
      if (!validShape) {
        throw new Error('Not a valid Inkwest config export.');
      }
      setPending(parsed);
    } catch (e) {
      setPending(null);
      setParseError((e as Error).message);
      if (fileRef.current) fileRef.current.value = ''; // let the user re-pick the same file
    }
  };

  const confirmImport = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await importConfig(pending, replaceAll);
      setDone(`Imported ${pending.skills.length} skill(s)${replaceAll ? ' (replaced all)' : ''}.`);
      setPending(null);
      if (fileRef.current) fileRef.current.value = '';
    } catch {
      // surfaced via store error
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h4 className="text-sm font-semibold text-gray-200">Export</h4>
        <p className="text-xs text-gray-500">
          Download your display settings + personal skills as a versioned JSON file.
        </p>
        <Button variant="secondary" onClick={() => void handleExport()}>
          <Download className="w-4 h-4 mr-2" />
          Export config
        </Button>
      </section>

      <section className="space-y-2">
        <h4 className="text-sm font-semibold text-gray-200">Import</h4>
        <p className="text-xs text-gray-500">
          Load a previously exported file. Review the summary before applying — import only
          ever changes your own account.
        </p>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void handleFile(f);
          }}
          className="block text-sm text-gray-400 file:mr-3 file:rounded file:border-0 file:bg-gray-700 file:px-3 file:py-1.5 file:text-gray-200 hover:file:bg-gray-600"
        />
        {parseError && <p className="text-xs text-red-400">{parseError}</p>}

        {pending && (
          <div className="rounded border border-gray-700 bg-gray-900/40 p-3 space-y-3">
            <div className="text-sm text-gray-300">
              Ready to import: <strong>{pending.skills.length}</strong> skill(s), schema
              version <strong>{pending.schema_version}</strong>.
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={replaceAll}
                onChange={(e) => setReplaceAll(e.target.checked)}
              />
              Replace all my personal skills (delete any not in this file)
            </label>
            {error && <p className="text-xs text-red-400">{error}</p>}
            <div className="flex gap-2">
              <Button onClick={() => void confirmImport()} disabled={busy}>
                <Upload className="w-4 h-4 mr-2" />
                {replaceAll ? 'Replace & import' : 'Merge & import'}
              </Button>
              <Button variant="secondary" onClick={() => setPending(null)}>
                Cancel
              </Button>
            </div>
          </div>
        )}
        {done && <p className="text-xs text-green-400">{done}</p>}
      </section>
    </div>
  );
}
