import { useEffect, useState } from 'react';
import { getMcpCatalog, type McpServerEntry } from '@/services/api';
import { McpToolPolicyEditor } from '@/components/connector/McpToolPolicyEditor';

/**
 * Global MCP tool-permission defaults (per user, across all conversations). Pick a server,
 * then set each tool's default verdict (allow / deny / ask). These are the defaults a
 * conversation inherits unless it sets its own override.
 */
export function McpPolicySettingsTab() {
  const [servers, setServers] = useState<McpServerEntry[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMcpCatalog()
      .then((cat) => {
        if (cancelled) return;
        // Only servers usable server-side are enumerable (unavailable = sandbox disabled).
        const usable = cat.servers.filter((s) => s.auth_status !== 'unavailable');
        setServers(usable);
        setSelected((prev) => prev || usable[0]?.key || '');
      })
      .catch(() => !cancelled && setError('Failed to load MCP servers'));
    return () => {
      cancelled = true;
    };
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
      <label className="flex items-center gap-2 text-sm text-gray-300">
        <span>Server</span>
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        >
          {servers.map((s) => (
            <option key={s.key} value={s.key}>
              {s.key}
            </option>
          ))}
        </select>
      </label>
      {selected && <McpToolPolicyEditor serverKey={selected} scope="global" />}
    </section>
  );
}
