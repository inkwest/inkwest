import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Ban, Loader2, Plug, PlugZap, Settings } from 'lucide-react';
import { McpIcon } from '@/components/icons/BrandIcons';
import { ConnectorIconButton } from './ConnectorIconButton';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { ToggleSwitch } from '@/components/ui/ToggleSwitch';
import {
  disconnectMcpAuth,
  getSessionMcp,
  setSessionMcp,
  type McpServerEntry,
} from '@/services/api';
import { McpToolPolicyEditor } from './McpToolPolicyEditor';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { useSessionsStore } from '@/store/sessions';
import { useMcpStore } from '@/store/mcp';
import { useSettingsUi } from '@/store/settingsUi';
import { loadMcpCatalog, peekMcpCatalog, primeMcpCatalog } from './mcpCatalogCache';

// A `mcp_catalog_changed` broadcast hits every connected client at once; each mounted McpPanel
// would otherwise re-fetch the catalog simultaneously (a synchronized spike on one endpoint).
// Delay the live-reload by a random 0..N ms so the fetches smear across the herd. Small enough
// to still feel live; only the WS-triggered reload is jittered (mount/open fetches are already
// spread by user action).
const CATALOG_RELOAD_JITTER_MS = 1000;

// One server row in the modal's left list: the whole row selects (slides out its tools); the
// toggle and the right-side controls (status/cog) act on their own (stopPropagation).
function ServerRow({
  label,
  selected,
  onSelect,
  control,
  right,
  dimmed = false,
}: {
  label: string;
  selected: boolean;
  onSelect: () => void;
  // The enable/disable ToggleSwitch (acts on its own; stopPropagation inside).
  control: ReactNode;
  right: ReactNode;
  // Fades the label while the server is not enabled for this conversation.
  dimmed?: boolean;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect();
        }
      }}
      className={`flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-2 ${
        selected ? 'bg-blue-600/30' : 'hover:bg-gray-800'
      }`}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {control}
        <span className={`truncate text-sm ${dimmed ? 'text-gray-500' : 'text-gray-200'}`}>
          {label}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1" onClick={(e) => e.stopPropagation()}>
        {right}
      </div>
    </div>
  );
}

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
 * MCP servers, one per line with an enable toggle. Toggling persists the selection and
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
  // Mirror the per-session selection into the shared store so ChatInput's "no connector
  // activated" warning reacts to MCP enablement without its own fetch.
  const setSessionEnabled = useMcpStore((s) => s.setSessionEnabled);
  // Deep-link to the global MCP tool-permission settings (opens Settings on the 'mcp' tab).
  const openSettings = useSettingsUi((s) => s.open);
  // Bumped by the sync socket on an `mcp_catalog_changed` broadcast (the cache is reset there
  // first). Drives a catalog-*set*-only refresh (see the live-reload effect below), deliberately
  // NOT the mount effect: the session selection must not be refetched on a global catalog change
  // (it would reset an open modal's unsaved draft).
  const catalogVersion = useMcpStore((s) => s.catalogVersion);
  // Active connect-poll intervals, tracked so they're always cleared on unmount (otherwise a
  // popup left open would poll the catalog forever and setState on an unmounted component).
  const pollTimers = useRef<Set<number>>(new Set());
  // Keys with an in-flight connect poll, so a double-click doesn't spawn a second popup+poll.
  const connecting = useRef<Set<string>>(new Set());
  // Reactive mirror of `connecting` for rendering the transient "connecting…" status icon
  // (the ref alone can't drive a re-render).
  const [connectingKeys, setConnectingKeys] = useState<Set<string>>(new Set());
  // Server selected in the modal's left list → its tools slide out on the right (one at a time).
  const [selectedServer, setSelectedServer] = useState<string | null>(null);
  // The server the right panel is rendering. Lags `selectedServer` on a SWITCH: the content stays
  // mounted while the panel retracts (so the shrink is visible with content), then swaps + reopens.
  const [shownServer, setShownServer] = useState<string | null>(null);
  // Panel width state (open = expanded). Separate from shownServer so the retract animates with
  // the OLD content still shown, then we swap content and expand.
  const [panelOpen, setPanelOpen] = useState(false);
  // Monotonic guard shared by every catalog fetch (mount load, live-reload, popup-open): all
  // force the same live endpoint, so an earlier-issued request resolving last must not clobber a
  // fresher one's result. Only the latest-sequenced fetch writes catalog/oauthServers state.
  const catalogSeq = useRef(0);
  // Last catalogVersion the live-reload effect acted on. Seeded from the current value so the
  // effect's initial run (and StrictMode's dev double-invoke) is a no-op — the mount effect
  // already loads the catalog; only a genuine *later* bump triggers a reload.
  const seenCatalogVersion = useRef(catalogVersion);

  // Force-fetch the catalog *set* (keys + oauth servers), seq-guarded so a superseded response
  // yields null instead of clobbering fresher state. Never touches the session selection/draft.
  const fetchCatalogSet = useCallback(async () => {
    const seq = ++catalogSeq.current;
    const cat = await loadMcpCatalog(true);
    if (seq !== catalogSeq.current) return null; // a newer fetch already ran
    return { keys: cat.keys, oauth: cat.servers.filter((s) => s.kind === 'oauth2') };
  }, []);

  useEffect(() => {
    const timers = pollTimers.current;
    return () => timers.forEach((t) => window.clearInterval(t));
  }, []);

  // Mount / session-switch load: force-revalidate the catalog (stale-while-revalidate — the
  // synchronous `peek` seed above still renders the last-known catalog instantly, no flicker)
  // AND load this session's selection. Keyed on sessionId only; a global catalog change does
  // NOT re-run this (that would refetch the selection and reset an open modal's unsaved draft).
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const set = await fetchCatalogSet();
        if (cancelled) return;
        if (set) {
          setCatalog(set.keys);
          setOauthServers(set.oauth);
        }

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
          setSessionEnabled(sessionId, sel.enabled);
        }
      } catch {
        if (!cancelled) setError('Failed to load MCP servers');
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [sessionId, setSessionEnabled, fetchCatalogSet]);

  // Live reload on a WS `mcp_catalog_changed` broadcast: refresh the catalog *set* only, so the
  // toolbar badge and (if open) the modal reflect the change without touching the session
  // selection/draft. Skips its initial run — the mount effect already loaded on first render.
  useEffect(() => {
    if (seenCatalogVersion.current === catalogVersion) return; // mount / StrictMode replay — no-op
    seenCatalogVersion.current = catalogVersion;
    let cancelled = false;
    // Jitter: the broadcast is synchronized across all clients, so spread the re-fetch.
    const timer = window.setTimeout(() => {
      void fetchCatalogSet().then((set) => {
        if (cancelled || !set) return;
        setCatalog(set.keys);
        setOauthServers(set.oauth);
      });
    }, Math.random() * CATALOG_RELOAD_JITTER_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [catalogVersion, fetchCatalogSet]);

  // Revalidate the catalog whenever the modal opens: a panel mounted for a long time may hold
  // a stale cache and have missed the WS bump (reconnect gap). Server *set* only — the
  // selection is owned by the mount effect above. Failure keeps the last-known catalog.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void fetchCatalogSet().then((set) => {
      if (cancelled || !set) return;
      setCatalog(set.keys);
      setOauthServers(set.oauth);
    });
    return () => {
      cancelled = true;
    };
  }, [open, fetchCatalogSet]);

  // Drive the retract-then-reopen on a server switch. Depends ONLY on selectedServer so it fires
  // just once per selection change: collapse the panel (shownServer=null), then reopen with the
  // new server after the slide. First open (panel already collapsed) reopens immediately; a real
  // switch waits the transition so the retract is visible. (shownServer is read but intentionally
  // not a dep — adding it would re-fire on the collapse and skip the retract.)
  useEffect(() => {
    if (selectedServer === null) {
      setPanelOpen(false); // deselect/close → retract
      return;
    }
    if (shownServer === null) {
      // first open: mount content + expand.
      setShownServer(selectedServer);
      setPanelOpen(true);
      return;
    }
    if (selectedServer === shownServer) {
      setPanelOpen(true); // re-select the shown server → ensure open
      return;
    }
    // Real switch: retract with the OLD content still shown, then swap content + reopen after
    // the transition (> duration-200) so the shrink is fully visible.
    setPanelOpen(false);
    const t = window.setTimeout(() => {
      setShownServer(selectedServer);
      setPanelOpen(true);
    }, 260);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedServer]);

  const toggleDraft = (key: string) => {
    if (saving) return;
    const next = new Set(draftEnabled);
    next.has(key) ? next.delete(key) : next.add(key);
    setDraftEnabled(next);
  };

  const handleClose = () => {
    setDraftEnabled(new Set(enabled));
    setError(null);
    setSelectedServer(null);
    setShownServer(null);
    setPanelOpen(false);
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
      setSessionEnabled(sessionId, res.enabled);
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
      const seq = ++catalogSeq.current;
      const cat = await loadMcpCatalog(true);
      const oauth = cat.servers.filter((s) => s.kind === 'oauth2');
      // Share the seq guard with the catalog effects so a concurrent live-reload/open fetch and
      // this poll can't clobber each other's write; the poll's own return value is unaffected.
      if (seq === catalogSeq.current) {
        setCatalog(cat.keys);
        setOauthServers(oauth);
      }
      return oauth;
    } catch {
      /* transient; the panel keeps the last known statuses */
      return [];
    }
  };

  const connectOauth = (key: string) => {
    // Clear any stale error BEFORE the in-flight guard: a user re-clicking Connect must always get
    // feedback (error gone), even if a prior attempt's poll still holds the key and we bail below.
    setError(null);
    if (connecting.current.has(key)) return; // a poll is already in flight for this key
    const popup = openMcpAuthPopup(key, (message) =>
      setError(`Failed to start MCP authorization: ${message}`),
    );
    connecting.current.add(key);
    setConnectingKeys((prev) => new Set(prev).add(key));
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
        setConnectingKeys((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        // Close the popup from the opener once connected — reliable, unlike the
        // callback's own window.close() (blocked after the cross-origin round-trip,
        // and blocked outright by the prod CSP script-src 'self').
        if (connected) {
          setError(null); // a late success clears an error raised earlier this attempt
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
        setSessionEnabled(sessionId, res.enabled);
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

  // Per-server cog: opens the global tool-permission settings (preselecting this server) STACKED
  // on top of this popup — closing Settings returns here. Present even for servers without a
  // connection (placed right of the status icon).
  const policyCog = (key: string) => (
    <button
      type="button"
      onClick={() => openSettings('mcp', key)}
      title="Tool permissions"
      aria-label={`Tool permissions for ${key}`}
      className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
    >
      <Settings className="h-3.5 w-3.5" />
    </button>
  );

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

      <Modal
        isOpen={open && hasCatalog}
        onClose={handleClose}
        title="MCP servers"
        size="xl"
        // Height-constrained layout: intro + footer stay pinned, the two-pane
        // area is the buffer that shrinks/scrolls — OK never goes below the fold.
        bodyClassName="flex min-h-0 flex-col overflow-hidden"
      >
        <p className="mb-3 shrink-0 text-sm text-gray-400">
          Enable MCP servers for this conversation; select one to set its per-tool permissions.
        </p>
        <div className="flex h-[520px] min-h-0 gap-3">
          {/* Left: server list — the toggle enables, the row selects (slides the tools out). */}
          {/* pl-1 mirrors the tools pane's p-1 (veil padding) so both outer margins match. */}
          <div className="w-64 shrink-0 space-y-1 overflow-y-auto pl-1 pr-1">
            {catalog.map((key) => (
              <ServerRow
                key={key}
                label={key}
                selected={selectedServer === key}
                onSelect={() => setSelectedServer(key)}
                dimmed={!draftEnabled.has(key)}
                control={
                  <ToggleSwitch
                    aria-label={key}
                    checked={draftEnabled.has(key)}
                    disabled={saving}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleDraft(key)}
                  />
                }
                right={policyCog(key)}
              />
            ))}

            {oauthServers.length > 0 && (
              <div className="mt-3 border-t border-gray-700 pt-2">
                <div className="mb-1 px-2 text-xs font-medium text-gray-400">OAuth servers</div>
                {oauthServers.map((s) => {
                  const connected = s.auth_status === 'connected';
                  const needsAuth = s.auth_status === 'needs_auth';
                  const unavailable = s.auth_status === 'unavailable';
                  const isConnecting = connectingKeys.has(s.key);
                  // Fuse connection state + action into one icon: color signals the state, the
                  // click runs the matching action, full text lives in title/aria-label.
                  let StatusIcon = Plug;
                  let iconClass = 'text-gray-500';
                  let statusLabel = `${s.key}: not connected — click to connect`;
                  let onStatusClick: (() => void) | undefined = () => connectOauth(s.key);
                  let statusDisabled = saving;
                  if (isConnecting) {
                    StatusIcon = Loader2;
                    iconClass = 'text-yellow-400 animate-spin';
                    statusLabel = `${s.key}: connecting… — click to retry`;
                    onStatusClick = () => connectOauth(s.key);
                  } else if (unavailable) {
                    StatusIcon = Ban;
                    iconClass = 'text-gray-600';
                    statusLabel = `${s.key}: unavailable — sandbox required`;
                    onStatusClick = undefined;
                    statusDisabled = true;
                  } else if (connected) {
                    StatusIcon = PlugZap;
                    iconClass = 'text-green-400';
                    statusLabel = `${s.key}: connected — click to disconnect`;
                    onStatusClick = () => void disconnectOauth(s.key);
                  } else if (needsAuth) {
                    StatusIcon = Plug;
                    iconClass = 'text-yellow-400';
                    statusLabel = `${s.key}: needs auth — click to reconnect`;
                    onStatusClick = () => connectOauth(s.key);
                  }
                  return (
                    <ServerRow
                      key={s.key}
                      label={s.key}
                      selected={selectedServer === s.key}
                      onSelect={() => setSelectedServer(s.key)}
                      dimmed={!draftEnabled.has(s.key)}
                      control={
                        <ToggleSwitch
                          aria-label={s.key}
                          checked={draftEnabled.has(s.key)}
                          disabled={saving || !connected}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => toggleDraft(s.key)}
                        />
                      }
                      right={
                        <>
                          <button
                            type="button"
                            onClick={onStatusClick}
                            disabled={statusDisabled}
                            title={statusLabel}
                            aria-label={statusLabel}
                            className="rounded p-1 hover:bg-gray-700 disabled:cursor-default disabled:hover:bg-transparent"
                          >
                            <StatusIcon className={`h-4 w-4 ${iconClass}`} />
                          </button>
                          {/* Cog rightmost — consistent even for servers without a status icon. */}
                          {policyCog(s.key)}
                        </>
                      }
                    />
                  );
                })}
              </div>
            )}
          </div>

          {/* Right: slides out when a server is selected — its per-conversation tool permissions. */}
          <div
            className={`overflow-hidden transition-all duration-200 ${
              panelOpen ? 'w-[560px] border-l border-gray-700 pl-3 opacity-100' : 'w-0 opacity-0'
            }`}
          >
            {shownServer && (
              // p-1 keeps the content 4px inside the pane so the veil's rounded corners
              // (which cover the padding box edge-to-edge) can never reveal text beneath.
              <div className="relative h-full p-1">
                <div className="flex h-full min-h-0 flex-col">
                  <div className="mb-2 shrink-0 text-xs font-medium text-gray-400">
                    Tools — {shownServer} (this conversation)
                  </div>
                  {sessionId ? (
                    <div className="min-h-0 flex-1">
                      <McpToolPolicyEditor
                        key={shownServer}
                        serverKey={shownServer}
                        scope="session"
                        sessionId={sessionId}
                      />
                    </div>
                  ) : (
                    <p className="text-xs text-gray-500">
                      Start the conversation to set per-tool permissions.
                    </p>
                  )}
                </div>
                {/* Disabled server: veil the whole pane — permissions only matter once the
                    server is enabled, and the veil points at the toggle as the next step. */}
                {!draftEnabled.has(shownServer) && (
                  <div className="absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-gray-900/75">
                    <p className="max-w-[240px] px-4 text-center text-sm text-gray-300">
                      This MCP is off for this conversation — turn it on to use its tools.
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {error && <p className="mt-3 shrink-0 text-sm text-red-400">{error}</p>}
        <div className="mt-4 flex shrink-0 items-center justify-between gap-3 border-t border-gray-700 pt-3">
          <span className="text-xs text-gray-500">
            {allKeys.filter((key) => draftEnabled.has(key)).length}/{allKeys.length} selected
            {' — '}changes apply to the next message
          </span>
          <Button size="sm" onClick={applySelection} disabled={!sessionId} isLoading={saving}>
            OK
          </Button>
        </div>
      </Modal>
    </>
  );
}
