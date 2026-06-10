import { useEffect, useState } from 'react';
import { Plug } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { getMcpCatalog, getSessionMcp, setSessionMcp } from '@/services/api';

interface Props {
  sessionId: string;
  // Optional hook run before the modal opens — used on the start screen to
  // lazily create the session (so the selection has somewhere to persist).
  onBeforeOpen?: () => Promise<void> | void;
  // 'inline' (default): compact button for the in-conversation connector bar.
  // 'row': full-width connector-style row (start screen, stacked layout).
  variant?: 'inline' | 'row';
  // Open the modal as soon as this becomes true. Used to reopen after the start
  // screen lazily creates the session (which remounts this component).
  autoOpen?: boolean;
  // Skip opening here on click (the caller reopens elsewhere, e.g. via autoOpen
  // on the post-session instance). Avoids a flash on the soon-to-unmount instance.
  deferOpen?: boolean;
}

/**
 * Per-conversation MCP selector. Opens a modal listing the catalog of available
 * MCP servers, one per line with a checkbox. Toggling persists the selection and
 * re-merges the conversation's .claude.json server-side; it takes effect on the
 * NEXT message (the CLI reads its config at launch). Renders either as a compact
 * button ('inline') or as a connector-style row ('row').
 */
export function McpPanel({
  sessionId,
  onBeforeOpen,
  variant = 'inline',
  autoOpen = false,
  deferOpen = false,
}: Props) {
  const [catalog, setCatalog] = useState<string[]>([]);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(autoOpen);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // No session yet (start screen before first interaction) — nothing to fetch.
    if (!sessionId) return;
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

  // In-conversation bar: no catalog configured → render nothing. On the start
  // screen (onBeforeOpen set) the button shows before the session exists, so we
  // keep it even with an empty catalog (it loads once the session is created).
  if (catalog.length === 0 && !onBeforeOpen) return null;

  const count = enabled.size;
  const anyOn = count > 0;

  const handleOpen = async () => {
    if (onBeforeOpen) await onBeforeOpen();
    if (!deferOpen) setOpen(true);
  };

  return (
    <>
      {variant === 'row' ? (
        // Connector-style full-width row (matches the "External access" rows).
        <div className="flex w-full items-center justify-between rounded-md px-2 py-1">
          <div className="flex items-center gap-2">
            <Plug className="h-4 w-4 text-gray-400" />
            <span className="text-sm text-gray-200">MCP servers</span>
            {anyOn ? (
              <Badge variant="success">
                {count}/{catalog.length} enabled
              </Badge>
            ) : (
              <Badge variant="default">none</Badge>
            )}
          </div>
          <button
            onClick={handleOpen}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            Configure
          </button>
        </div>
      ) : (
        // Compact button for the in-conversation connector bar.
        <button
          onClick={handleOpen}
          title="Choose which MCP servers this conversation can use"
          className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-sm border transition-colors ${
            anyOn
              ? 'border-green-600 bg-green-900/30 text-green-300'
              : 'border-gray-700 text-gray-300 hover:bg-gray-800'
          }`}
        >
          <Plug className="h-3.5 w-3.5" />
          MCP{catalog.length ? ` ${count}/${catalog.length}` : ''}
        </button>
      )}

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
