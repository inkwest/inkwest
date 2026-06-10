import { useEffect, useState } from 'react';
import { Plug } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { getMcpCatalog, getSessionMcp, setSessionMcp } from '@/services/api';

function expiryLabel(expiresAt?: string | null): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const mins = Math.round(ms / 60000);
  return mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60}m left` : `${mins}m left`;
}

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
  // Keep the control visible even when the MCP catalog is empty. Used by the
  // start screen after it lazily creates a session from the MCP row.
  showWhenEmpty?: boolean;
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
  showWhenEmpty = false,
}: Props) {
  const [catalog, setCatalog] = useState<string[]>([]);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(autoOpen);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const cat = await getMcpCatalog();
        if (cancelled) return;
        setCatalog(cat.keys);

        if (!sessionId) {
          setEnabled(new Set());
          setExpiresAt(null);
          return;
        }

        const sel = await getSessionMcp(sessionId);
        if (!cancelled) {
          setEnabled(new Set(sel.enabled));
          setExpiresAt(sel.expires_at ?? null);
        }
      } catch {
        if (!cancelled) setError('Failed to load MCP servers');
      }
    };
    void load();
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
      setExpiresAt(res.expires_at ?? null);
    } catch {
      setEnabled(previous);
      setError('Failed to update MCP selection');
    } finally {
      setSaving(null);
    }
  };

  // In-conversation bar: no catalog configured -> render nothing. On the start
  // screen the row remains visible before and after lazy session creation, so a
  // click on Configure never makes the MCP affordance vanish.
  if (catalog.length === 0 && !onBeforeOpen && !showWhenEmpty) return null;

  const hasCatalog = catalog.length > 0;
  const enabledKeys = catalog.filter((key) => enabled.has(key));
  const count = enabledKeys.length;
  const anyOn = count > 0;
  const exp = expiryLabel(expiresAt);
  const badgeText = exp ? `${count}/${catalog.length} enabled · ${exp}` : `${count}/${catalog.length} enabled`;

  const handleOpen = async () => {
    if (!hasCatalog) return;
    if (onBeforeOpen) await onBeforeOpen();
    if (!deferOpen) setOpen(true);
  };

  return (
    <>
      {variant === 'row' ? (
        // Connector-style full-width row (matches the "External access" rows).
        <div className="group relative flex w-full items-center justify-between rounded-md px-2 py-1">
          <div className="flex items-center gap-2">
            <Plug className="h-4 w-4 text-gray-400" />
            <span className="text-sm text-gray-200">MCP servers</span>
            {hasCatalog ? (
              <Badge variant={anyOn ? 'success' : 'default'}>{badgeText}</Badge>
            ) : (
              <Badge variant="default">none</Badge>
            )}
          </div>
          {hasCatalog && (
            <button
              onClick={handleOpen}
              className="text-xs text-blue-400 hover:text-blue-300"
            >
              Configure
            </button>
          )}
          <div className="pointer-events-none absolute left-0 top-full z-50 mt-1 hidden w-60 rounded-md border border-gray-700 bg-gray-800 p-2 text-xs shadow-lg group-hover:block">
            <div className="mb-1 font-medium text-gray-300">Permissions</div>
            {enabledKeys.length > 0 ? (
              <ul className="space-y-1 text-gray-400">
                {enabledKeys.map((key) => (
                  <li key={key}>
                    <span className="truncate text-gray-300">{key}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-gray-400">No access granted</div>
            )}
          </div>
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

      <Modal isOpen={open && hasCatalog} onClose={() => setOpen(false)} title="MCP servers" size="sm">
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
