import { useEffect, useState } from 'react';
import { Box } from 'lucide-react';
import { Button, Card, CardHeader, CardTitle, CardContent, Input } from '@/components/ui';
import { getSandboxSettings, updateSandboxSettings } from '@/services/adminApi';

// Editable runtime sandbox limits. Self-contained (own fetch/save state) so it can drop
// into the otherwise read-only System Info page without touching its store. Currently the
// per-user concurrent-sandbox cap; the value takes effect immediately (no restart).
export function SandboxSettingsCard() {
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
    <Card variant="bordered">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Box className="w-5 h-5 text-blue-400" />
          Sandbox limits
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : (
          <div className="space-y-3 max-w-md">
            <div className="flex items-end gap-3">
              <Input
                type="number"
                min={0}
                label="Max conversations per user"
                value={value}
                onChange={(e) => {
                  setValue(e.target.value);
                  setSaved(false);
                }}
                error={!valid ? 'Enter a whole number ≥ 0' : undefined}
                className="w-32"
              />
              <Button onClick={save} isLoading={saving} disabled={!valid || saving}>
                Save
              </Button>
            </div>
            <p className="text-xs text-gray-500">
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
            </p>
            {saved && <p className="text-xs text-green-400">Saved — effective immediately.</p>}
            {error && <p className="text-xs text-red-400">{error}</p>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
