import { useEffect, useState } from 'react';
import { getMcpCatalog, type McpServerEntry } from '@/services/api';
import { McpToolPolicyEditor } from '@/components/connector/McpToolPolicyEditor';
import { useSettingsUi } from '@/store/settingsUi';

/**
 * Global MCP tool-permission defaults (per user, across all conversations). Two-column
 * master–detail: pick a server on the left, edit its per-tool defaults on the right.
 */
export function McpPolicySettingsTab() {
  const [servers, setServers] = useState<McpServerEntry[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  // A per-server cog deep-links here with the server to preselect (consumed once on mount).
  const initialServer = useSettingsUi((s) => s.initialServer);

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

  return (
    <section className="space-y-3">
      <p className="text-sm text-gray-400">
        Default permission for each MCP tool. A conversation can override these; unset tools
        fall back to “Ask each time”.
      </p>
      <div className="flex gap-3">
        {/* Left: server list — selection drives the right pane. */}
        <ul className="w-44 shrink-0 space-y-0.5 border-r border-gray-700 pr-2" role="listbox" aria-label="MCP servers">
          {servers.map((s) => (
            <li key={s.key}>
              <button
                type="button"
                role="option"
                aria-selected={selected === s.key}
                onClick={() => setSelected(s.key)}
                className={`w-full truncate rounded px-2 py-1.5 text-left text-sm ${
                  selected === s.key
                    ? 'bg-blue-600/30 text-white'
                    : 'text-gray-300 hover:bg-gray-800'
                }`}
                title={s.key}
              >
                {s.key}
              </button>
            </li>
          ))}
        </ul>
        {/* Right: the selected server's per-tool defaults. */}
        <div className="min-w-0 flex-1">
          {selected && <McpToolPolicyEditor key={selected} serverKey={selected} scope="global" />}
        </div>
      </div>
    </section>
  );
}
