import { useEffect, useState } from 'react';
import { Loader2, Plug } from 'lucide-react';
import { getMcpCatalog, type McpAuthStatus, type McpServerEntry } from '@/services/api';
import { McpToolPolicyEditor } from '@/components/connector/McpToolPolicyEditor';
import { ResizableDivider } from '@/components/ui/ResizableDivider';
import { useSettingsUi } from '@/store/settingsUi';
import { useMcpOauthConnect } from '@/hooks/useMcpOauthConnect';

// OAuth2 servers in these states can't be enumerated — listing their tools would 500 with a
// vague "unreachable or not connected". Short-circuit to a Connect prompt instead.
const NEEDS_CONNECT: ReadonlySet<McpAuthStatus> = new Set(['needs_auth', 'not_connected']);

/**
 * Global MCP tool-permission defaults (per user, across all conversations). Two-column
 * master–detail: pick a server on the left, edit its per-tool defaults on the right. An OAuth2
 * server that isn't connected shows a Connect prompt instead of a dead-end tool-listing error.
 */
export function McpPolicySettingsTab() {
  const [servers, setServers] = useState<McpServerEntry[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  // A per-server cog deep-links here with the server to preselect (consumed once on mount).
  const initialServer = useSettingsUi((s) => s.initialServer);
  // Draggable split: width of the left server list (px). The right editor flex-fills the rest.
  const LIST_W_DEFAULT = 224;
  const [listWidth, setListWidth] = useState(LIST_W_DEFAULT);
  const resizeList = (dx: number) =>
    setListWidth((w) => Math.min(Math.max(w + dx, 160), 400));

  // A finished connect returns the refreshed catalog — swap it in so the selected server flips
  // to "connected" and its tool editor renders in place of the Connect prompt automatically.
  const { connect, connectingKeys, error: connectError } = useMcpOauthConnect((next) =>
    setServers(next.filter((s) => s.auth_status !== 'unavailable'))
  );

  useEffect(() => {
    let cancelled = false;
    getMcpCatalog()
      .then((cat) => {
        if (cancelled) return;
        // Only servers usable server-side are enumerable (unavailable = sandbox disabled).
        const usable = cat.servers.filter((s) => s.auth_status !== 'unavailable');
        setServers(usable);
        const preselect =
          initialServer && usable.some((s) => s.key === initialServer) ? initialServer : '';
        setSelected((prev) => prev || preselect || usable[0]?.key || '');
      })
      .catch(() => !cancelled && setError('Failed to load MCP servers'));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (servers.length === 0)
    return <p className="text-sm text-gray-400">No MCP servers configured.</p>;

  const selectedServer = servers.find((s) => s.key === selected);
  const needsConnect =
    selectedServer?.kind === 'oauth2' && NEEDS_CONNECT.has(selectedServer.auth_status);
  const isConnecting = connectingKeys.has(selected);

  return (
    <section className="flex h-full min-h-0 flex-col gap-3">
      <p className="shrink-0 text-sm text-gray-400">
        Default permission for each MCP tool. A conversation can override these; unset tools
        fall back to “Ask each time”.
      </p>
      {/* Bounded height so each pane scrolls on its own; min-h-0 lets the panes shrink
          (they are the scroll buffer) instead of pushing the modal past the viewport. */}
      <div className="flex h-[520px] min-h-0">
        {/* Left: server list — selection drives the right pane. */}
        <ul
          style={{ width: listWidth }}
          className="shrink-0 space-y-0.5 overflow-y-auto pr-3"
          role="listbox"
          aria-label="MCP servers"
        >
          {servers.map((s) => {
            const unconnected = s.kind === 'oauth2' && NEEDS_CONNECT.has(s.auth_status);
            // Consistent status pastille on EVERY row: amber = needs connecting, green = ready
            // (a connected OAuth grant, or a static server that never needs one).
            const dotClass = unconnected ? 'bg-yellow-400' : 'bg-green-400';
            const dotLabel = unconnected
              ? 'not connected'
              : s.kind === 'oauth2'
                ? 'connected'
                : 'available';
            return (
              <li key={s.key}>
                <button
                  type="button"
                  role="option"
                  aria-selected={selected === s.key}
                  onClick={() => setSelected(s.key)}
                  className={`flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-sm ${
                    selected === s.key
                      ? 'bg-blue-600/30 text-white'
                      : 'text-gray-300 hover:bg-gray-800'
                  }`}
                  title={`${s.key} — ${dotLabel}`}
                >
                  <span
                    aria-hidden
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotClass}`}
                  />
                  <span className="truncate">{s.key}</span>
                </button>
              </li>
            );
          })}
        </ul>
        {/* Draggable split — double-click resets to the default list width. */}
        <ResizableDivider
          onResize={resizeList}
          onReset={() => setListWidth(LIST_W_DEFAULT)}
          label="Resize server list"
        />
        {/* Right: the selected server's per-tool defaults, or — when it needs auth — a Connect
            veil with a centred CTA (mirrors the conversation panel's veils). */}
        <div className="relative min-w-0 flex-1 pl-3">
          {selected &&
            (needsConnect ? (
              <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 rounded-lg bg-gray-900/75 text-center">
                <p className="max-w-xs px-4 text-sm text-gray-300">
                  <strong className="text-gray-100">{selected}</strong> isn’t connected. Connect it
                  to grant access and list its tools.
                </p>
                <button
                  type="button"
                  onClick={() => connect(selected)}
                  disabled={isConnecting}
                  className="inline-flex items-center gap-2 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-60"
                >
                  {isConnecting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Plug className="h-4 w-4" />
                  )}
                  {isConnecting ? 'Connecting…' : 'Connect'}
                </button>
                {connectError && <p className="text-xs text-red-400">{connectError}</p>}
              </div>
            ) : (
              <McpToolPolicyEditor key={selected} serverKey={selected} scope="global" />
            ))}
        </div>
      </div>
    </section>
  );
}
