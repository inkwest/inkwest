import { useEffect, useState } from 'react';
import { Plug } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { getMcpCatalog, getSessionMcp, setSessionMcp } from '@/services/api';

interface Props {
  sessionId: string;
}

/**
 * Per-conversation MCP selector. A compact "MCP" button (green when at least one
 * server is enabled) opens a modal listing the catalog of available MCP servers,
 * one per line with a checkbox. Toggling persists the selection and re-merges the
 * conversation's .claude.json server-side; it takes effect on the NEXT message
 * (the CLI reads its config at launch).
 */
export function McpPanel({ sessionId }: Props) {
  const [catalog, setCatalog] = useState<string[]>([]);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getMcpCatalog(), getSessionMcp(sessionId)])
      .then(([cat, sel]) => {
        if (cancelled) return;
        setCatalog(cat.keys);
        setEnabled(new Set(sel.enabled));
      })
      .catch(() => {
        if (!cancelled) setError('Failed to load MCP servers');
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const toggle = async (key: string) => {
    if (saving) return;
    const next = new Set(enabled);
    next.has(key) ? next.delete(key) : next.add(key);

    // Optimistic: reflect the toggle immediately, revert on failure.
    const previous = enabled;
    setEnabled(next);
    setSaving(key);
    setError(null);
    try {
      const res = await setSessionMcp(sessionId, [...next]);
      setEnabled(new Set(res.enabled));
    } catch {
      setEnabled(previous);
      setError('Failed to update MCP selection');
    } finally {
      setSaving(null);
    }
  };

  // Nothing to offer (no catalog configured) — render nothing.
  if (catalog.length === 0) return null;

  const count = enabled.size;
  const anyOn = count > 0;

  return (
    <>
      <span className="mx-1 h-5 w-px bg-gray-700" aria-hidden />
      <button
        onClick={() => setOpen(true)}
        title="Choose which MCP servers this conversation can use"
        className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-sm border transition-colors ${
          anyOn
            ? 'border-green-600 bg-green-900/30 text-green-300'
            : 'border-gray-700 text-gray-300 hover:bg-gray-800'
        }`}
      >
        <Plug className="h-3.5 w-3.5" />
        MCP {count}/{catalog.length}
      </button>

      <Modal isOpen={open} onClose={() => setOpen(false)} title="MCP servers" size="sm">
        <p className="text-sm text-gray-400 mb-3">
          Choose which MCP servers this conversation can use.
        </p>
        <div className="space-y-1">
          {catalog.map((key) => {
            const on = enabled.has(key);
            return (
              <label
                key={key}
                className="flex items-center gap-3 px-2 py-2 rounded cursor-pointer hover:bg-gray-800"
              >
                <input
                  type="checkbox"
                  checked={on}
                  disabled={saving !== null}
                  onChange={() => toggle(key)}
                  className="h-4 w-4 accent-green-600 disabled:opacity-50"
                />
                <span className="text-sm text-gray-200">{key}</span>
              </label>
            );
          })}
        </div>
        {error ? (
          <p className="text-sm text-red-400 mt-3">{error}</p>
        ) : (
          <p className="text-xs text-gray-500 mt-3">Changes apply to the next message.</p>
        )}
      </Modal>
    </>
  );
}
