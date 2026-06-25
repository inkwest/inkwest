import { useEffect, useRef, useState } from 'react';
import { McpIcon } from '@/components/icons/BrandIcons';
import { ConnectorIconButton } from './ConnectorIconButton';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import {
  disconnectMcpAuth,
  getSessionMcp,
  setSessionMcp,
  type McpServerEntry,
} from '@/services/api';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { useSessionsStore } from '@/store/sessions';
import { loadMcpCatalog, peekMcpCatalog, primeMcpCatalog } from './mcpCatalogCache';

interface Props {
  sessionId: string;
  // Optional hook run before the modal opens — used on the start screen to
  // lazily create the session (so the selection has somewhere to persist).
  onBeforeOpen?: () => Promise<void> | void;
  // 'inline' (default): connector-style item in the in-conversation connector bar.
  // 'row': full-width connector-style item (start screen, stacked layout).
  // 'icon': single square toolbar icon (composer bar) — tone + click-to-configure.
  variant?: 'inline' | 'row' | 'icon';
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
 * NEXT message (the CLI reads its config at launch). Renders as a connector-style
 * item in both the chat bar ('inline') and the start screen ('row').
 */
export function McpPanel({
  sessionId,
  onBeforeOpen,
  variant = 'inline',
  autoOpen = false,
  deferOpen = false,
  showWhenEmpty = false,
}: Props) {
  const [catalog, setCatalog] = useState<string[]>(() => peekMcpCatalog()?.keys ?? []);
  const [oauthServers, setOauthServers] = useState<McpServerEntry[]>(
    () => peekMcpCatalog()?.servers.filter((s) => s.kind === 'oauth2') ?? []
  );
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [draftEnabled, setDraftEnabled] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(autoOpen);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Active connect-poll intervals, tracked so they're always cleared on unmount (otherwise a
  // popup left open would poll the catalog forever and setState on an unmounted component).
  const pollTimers = useRef<Set<number>>(new Set());
  // Keys with an in-flight connect poll, so a double-click doesn't spawn a second popup+poll.
  const connecting = useRef<Set<string>>(new Set());

  useEffect(() => {
    const timers = pollTimers.current;
    return () => timers.forEach((t) => window.clearInterval(t));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const cat = await loadMcpCatalog();
        if (cancelled) return;
        setCatalog(cat.keys);
        setOauthServers(cat.servers.filter((s) => s.kind === 'oauth2'));

        if (!sessionId) {
          setEnabled(new Set());
          setDraftEnabled(new Set());
          return;
        }

        const sel = await getSessionMcp(sessionId);
        if (!cancelled) {
          const next = new Set(sel.enabled);
          setEnabled(next);
          setDraftEnabled(next);
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

  const toggleDraft = (key: string) => {
    if (saving) return;
    const next = new Set(draftEnabled);
    next.has(key) ? next.delete(key) : next.add(key);
    setDraftEnabled(next);
  };

  const handleClose = () => {
    setDraftEnabled(new Set(enabled));
    setError(null);
    setOpen(false);
  };

  const applySelection = async () => {
    if (saving || !sessionId) return;
    setSaving(true);
    setError(null);
    try {
      const res = await setSessionMcp(sessionId, [...draftEnabled]);
      const next = new Set(res.enabled);
      setEnabled(next);
      setDraftEnabled(next);
      setOpen(false);
      // Per-server connect/disconnect events were persisted server-side — reload the
      // conversation so they appear inline (flicker-free for the already-open session).
      void useSessionsStore.getState().fetchSession(sessionId);
    } catch {
      setError('Failed to update MCP selection');
    } finally {
      setSaving(false);
    }
  };

  const refreshOauth = async (): Promise<McpServerEntry[]> => {
    try {
      // Force-refresh: an OAuth connect just changed server state, so bypass the TTL.
      const cat = await loadMcpCatalog(true);
      setCatalog(cat.keys);
      const oauth = cat.servers.filter((s) => s.kind === 'oauth2');
      setOauthServers(oauth);
      return oauth;
    } catch {
      /* transient; the panel keeps the last known statuses */
      return [];
    }
  };

  const connectOauth = (key: string) => {
    if (connecting.current.has(key)) return; // a poll is already in flight for this key
    const popup = openMcpAuthPopup(key, () => setError('Failed to start MCP authorization'));
    connecting.current.add(key);
    // Poll the catalog while connecting; stop as soon as the grant lands ("connected"), the
    // popup closes, or a hard deadline elapses — so an abandoned popup can't poll forever.
    // The timer is tracked in pollTimers so unmount cleanup always clears it.
    const startedAt = Date.now();
    const POLL_DEADLINE_MS = 5 * 60 * 1000;
    const timer = window.setInterval(async () => {
      const servers = await refreshOauth();
      const connected = servers.find((s) => s.key === key)?.auth_status === 'connected';
      if (connected || !popup || popup.closed || Date.now() - startedAt > POLL_DEADLINE_MS) {
        window.clearInterval(timer);
        pollTimers.current.delete(timer);
        connecting.current.delete(key);
        // Close the popup from the opener once connected — reliable, unlike the
        // callback's own window.close() (blocked after the cross-origin round-trip,
        // and blocked outright by the prod CSP script-src 'self').
        if (connected) {
          try {
            popup?.close();
          } catch {
            /* already closed / cross-origin — nothing to do */
          }
        }
      }
    }, 2000);
    pollTimers.current.add(timer);
  };

  const disconnectOauth = async (key: string) => {
    try {
      // Drop it from the conversation selection FIRST: if the upstream revoke then fails, the
      // server is at least no longer enabled (consistent), rather than revoked-but-still-enabled
      // which would re-flag a reconnect every turn.
      if (sessionId && enabled.has(key)) {
        const next = [...enabled].filter((k) => k !== key);
        const res = await setSessionMcp(sessionId, next);
        const set = new Set(res.enabled);
        setEnabled(set);
        setDraftEnabled(set);
      }
      const cat = await disconnectMcpAuth(key);
      primeMcpCatalog(cat); // keep the shared cache in sync so a remount isn't stale
      setCatalog(cat.keys);
      setOauthServers(cat.servers.filter((s) => s.kind === 'oauth2'));
    } catch {
      setError('Failed to disconnect MCP server');
    }
  };

  // In-conversation bar: no catalog configured -> render nothing. On the start
  // screen the row remains visible before and after lazy session creation, so a
  // click on Configure never makes the MCP affordance vanish.
  if (catalog.length === 0 && oauthServers.length === 0 && !onBeforeOpen && !showWhenEmpty)
    return null;

  const hasCatalog = catalog.length > 0 || oauthServers.length > 0;
  // Count both static and connected oauth selections; the badge denominator is all selectable
  // keys, not just the static catalog (enabled oauth keys are persisted in `enabled` too).
  const allKeys = [...catalog, ...oauthServers.map((s) => s.key)];
  const enabledKeys = allKeys.filter((key) => enabled.has(key));
  const count = enabledKeys.length;
  const anyOn = count > 0;
  const badgeText = `${count}/${allKeys.length} enabled`;

  const handleOpen = async () => {
    if (!hasCatalog) return;
    if (onBeforeOpen) await onBeforeOpen();
    if (!deferOpen) {
      setDraftEnabled(new Set(enabled));
      setError(null);
      setOpen(true);
    }
  };

  return (
    <>
      {variant === 'icon' ? (
        // Toolbar icon: same green/gray tone scheme as the connector icons; click
        // opens the same config modal as the "Configure" link.
        <ConnectorIconButton
          Icon={McpIcon}
          title={hasCatalog ? `MCP servers — ${badgeText} (click to configure)` : 'MCP servers — none configured'}
          tone={anyOn ? 'on' : 'off'}
          onClick={handleOpen}
          disabled={!hasCatalog}
          overlayTitle="MCP servers"
          overlayBadge={
            hasCatalog ? (
              <Badge variant={anyOn ? 'success' : 'default'}>{badgeText}</Badge>
            ) : (
              <Badge variant="default">none</Badge>
            )
          }
          overlayBody={
            !hasCatalog ? (
              <div className="text-gray-400">No MCP servers configured.</div>
            ) : enabledKeys.length > 0 ? (
              <ul className="space-y-1 text-gray-400">
                {enabledKeys.map((key) => (
                  <li key={key}>
                    <span className="truncate text-gray-300">{key}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-gray-400">No servers enabled.</div>
            )
          }
        />
      ) : (
        <div
          className={`group relative flex items-center gap-2 rounded-md px-2 py-1 ${
            variant === 'row' ? 'w-full justify-between' : ''
          }`}
        >
          <div className="flex items-center gap-2">
            <McpIcon className="h-4 w-4 text-gray-400" />
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
      )}

      <Modal isOpen={open && hasCatalog} onClose={handleClose} title="MCP servers" size="sm">
        <p className="text-sm text-gray-400 mb-3">
          Choose which MCP servers this conversation can use.
        </p>
        <div className="space-y-1">
          {catalog.map((key) => {
            const on = draftEnabled.has(key);
            return (
              <label
                key={key}
                className="flex items-center gap-3 px-2 py-2 rounded cursor-pointer hover:bg-gray-800"
              >
                <input
                  type="checkbox"
                  checked={on}
                  disabled={saving}
                  onChange={() => toggleDraft(key)}
                  className="h-4 w-4 accent-green-600 disabled:opacity-50"
                />
                <span className="text-sm text-gray-200">{key}</span>
              </label>
            );
          })}
        </div>

        {oauthServers.length > 0 && (
          <div className="mt-4 border-t border-gray-700 pt-3">
            <div className="mb-2 text-xs font-medium text-gray-400">
              OAuth servers (connect once per account)
            </div>
            <div className="space-y-1">
              {oauthServers.map((s) => {
                const connected = s.auth_status === 'connected';
                const needsAuth = s.auth_status === 'needs_auth';
                const unavailable = s.auth_status === 'unavailable';
                return (
                  <div
                    key={s.key}
                    className="flex items-center justify-between gap-3 px-2 py-2 rounded hover:bg-gray-800"
                  >
                    <label className="flex items-center gap-2 cursor-pointer">
                      {/* Enable this conversation to use the server — only once connected.
                          Persisted with the static selection on OK; the backend injects the
                          proxy entry into .claude.json for enabled + connected oauth keys. */}
                      <input
                        type="checkbox"
                        checked={draftEnabled.has(s.key)}
                        disabled={saving || !connected}
                        onChange={() => toggleDraft(s.key)}
                        className="h-4 w-4 accent-green-600 disabled:opacity-40"
                      />
                      <span className="text-sm text-gray-200">{s.key}</span>
                      {connected && <Badge variant="success">connected</Badge>}
                      {needsAuth && <Badge variant="default">needs auth</Badge>}
                      {unavailable && <Badge variant="default">unavailable</Badge>}
                    </label>
                    {unavailable ? (
                      <span className="text-xs text-gray-500">sandbox required</span>
                    ) : connected ? (
                      <button
                        onClick={() => void disconnectOauth(s.key)}
                        className="text-xs text-red-400 hover:text-red-300"
                      >
                        Disconnect
                      </button>
                    ) : (
                      <button
                        onClick={() => connectOauth(s.key)}
                        className="text-xs text-blue-400 hover:text-blue-300"
                      >
                        {needsAuth ? 'Reconnect' : 'Connect'}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {error ? (
          <p className="text-sm text-red-400 mt-3">{error}</p>
        ) : (
          <p className="text-xs text-gray-500 mt-3">Changes apply to the next message.</p>
        )}
        <div className="mt-4 flex items-center justify-between gap-3 border-t border-gray-700 pt-3">
          <span className="text-xs text-gray-500">
            {allKeys.filter((key) => draftEnabled.has(key)).length}/{allKeys.length} selected
          </span>
          <Button size="sm" onClick={applySelection} disabled={!sessionId} isLoading={saving}>
            OK
          </Button>
        </div>
      </Modal>
    </>
  );
}
