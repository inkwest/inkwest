import { useEffect, useState } from 'react';
import { Info } from 'lucide-react';
import { Button, Input, Tooltip } from '@/components/ui';
import { getSandboxSettings, updateSandboxSettings } from '@/services/adminApi';

// Editable runtime sandbox limits (a section of the Sandbox card). Self-contained (own
// fetch/save state). Currently the per-user concurrent-sandbox cap; effective immediately.
export function SandboxLimitsSection() {
  const [value, setValue] = useState<string>('');
  const [envDefault, setEnvDefault] = useState<number | null>(null);
  const [hardCeiling, setHardCeiling] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const apply = (s: { max_sessions_per_user: number; hard_ceiling: number }) => {
    setValue(String(s.max_sessions_per_user));
    setHardCeiling(s.max_sessions_per_user > 0 ? s.hard_ceiling : null);
  };

  useEffect(() => {
    let active = true;
    getSandboxSettings()
      .then((s) => {
        if (!active) return;
        apply(s);
        setEnvDefault(s.default_max_sessions_per_user);
      })
      .catch((e) => active && setError(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  // Empty/whitespace is invalid (not silently coerced to 0/unlimited): a blank field is
  // an unfinished edit, not an intent to remove the cap. 0 must be typed explicitly.
  const parsed = Number(value);
  const valid = value.trim() !== '' && Number.isInteger(parsed) && parsed >= 0;

  const save = async () => {
    if (!valid) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const s = await updateSandboxSettings(parsed);
      apply(s);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h3 className="text-sm font-semibold text-gray-200 mb-3">Limits</h3>
      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : (
        <div className="space-y-3">
          {/* Label rendered here (not via Input) so it stays single-line above the narrow
              box, with the (i) help icon inline on the same text row. */}
          <div className="flex items-center gap-1.5">
            <label htmlFor="max-sessions-per-user" className="text-sm font-medium text-gray-300">
              Max conversations per user
            </label>
            <Tooltip
              content={
                <span>
                  Max concurrent conversation sandboxes per user (each = a container + proxy). Idle
                  conversations beyond it are reaped (they cold-start later); active ones are never
                  killed, so a burst is tolerated up to a hard ceiling, beyond which a new turn is
                  refused. <strong>0 = unlimited.</strong>
                  {hardCeiling !== null && (
                    <>
                      {' '}
                      At <strong>{value}</strong>: bursts allowed up to {hardCeiling} running, then
                      refused.
                    </>
                  )}
                  {envDefault !== null && ` Deployment default: ${envDefault}.`}
                </span>
              }
            >
              <Info
                className="w-4 h-4 text-gray-500 hover:text-gray-300"
                aria-label="About this limit"
              />
            </Tooltip>
          </div>
          <div className="flex items-start gap-2">
            <div className="w-32">
              <Input
                id="max-sessions-per-user"
                type="number"
                min={0}
                value={value}
                onChange={(e) => {
                  setValue(e.target.value);
                  setSaved(false);
                }}
                error={!valid ? 'Enter a whole number ≥ 0' : undefined}
              />
            </div>
            <Button onClick={save} isLoading={saving} disabled={!valid || saving}>
              Save
            </Button>
          </div>
          {saved && <p className="text-xs text-green-400">Saved — effective immediately.</p>}
          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>
      )}
    </div>
  );
}
