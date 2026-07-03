import { useCallback, useEffect, useState } from 'react';
import {
  getMcpServerTools,
  getMyMcpPolicy,
  getSessionMcpPolicy,
  setMyMcpPolicy,
  setSessionMcpPolicy,
  type McpPolicyEntry,
  type McpToolInfo,
  type McpVerdict,
} from '@/services/api';

// The server-wide default row is stored under tool '*'; tools inherit it unless overridden.
const SERVER_DEFAULT = '*';

// Session-scope dropdown: the 5 user-facing options mixing scope × verdict.
type Choice = 'always_allow' | 'conv_allow' | 'ask' | 'conv_deny' | 'always_deny';

const SESSION_OPTIONS: { value: Choice; label: string }[] = [
  { value: 'always_allow', label: 'Always allow' },
  { value: 'conv_allow', label: 'Allow this conversation' },
  { value: 'ask', label: 'Ask each time' },
  { value: 'conv_deny', label: 'Deny this conversation' },
  { value: 'always_deny', label: 'Always deny' },
];

// Global-scope dropdown: only the persistent defaults (no per-conversation notion here).
const GLOBAL_OPTIONS: { value: McpVerdict; label: string }[] = [
  { value: 'allow', label: 'Always allow' },
  { value: 'deny', label: 'Always deny' },
  { value: 'ask', label: 'Ask each time' },
];

type Scope = 'global' | 'session';

interface Props {
  serverKey: string;
  scope: Scope;
  // Required when scope === 'session'.
  sessionId?: string;
}

function toMap(entries: McpPolicyEntry[], serverKey: string): Record<string, McpVerdict> {
  const m: Record<string, McpVerdict> = {};
  for (const e of entries) if (e.server_key === serverKey) m[e.tool] = e.verdict;
  return m;
}

function toEntries(map: Record<string, McpVerdict>): { tool: string; verdict: McpVerdict }[] {
  return Object.entries(map).map(([tool, verdict]) => ({ tool, verdict }));
}

/**
 * Per-tool permission editor for one MCP server. In a conversation ('session' scope) each tool
 * gets the 5-option control (always/this-conversation × allow/deny, plus ask); a "global" option
 * writes the user default and clears any conversation override so it can't shadow the new default.
 * In the global settings scope only the 3 persistent defaults are offered.
 */
export function McpToolPolicyEditor({ serverKey, scope, sessionId }: Props) {
  const [tools, setTools] = useState<McpToolInfo[]>([]);
  const [globalMap, setGlobalMap] = useState<Record<string, McpVerdict>>({});
  const [convMap, setConvMap] = useState<Record<string, McpVerdict>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingTool, setSavingTool] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load = async () => {
      try {
        const [toolsRes, globalRes, convRes] = await Promise.all([
          getMcpServerTools(serverKey),
          getMyMcpPolicy(),
          scope === 'session' && sessionId
            ? getSessionMcpPolicy(sessionId)
            : Promise.resolve({ policies: [] as McpPolicyEntry[] }),
        ]);
        if (cancelled) return;
        setTools(toolsRes.tools);
        setGlobalMap(toMap(globalRes.policies, serverKey));
        setConvMap(toMap(convRes.policies, serverKey));
      } catch {
        if (!cancelled) setError('Failed to load tools or permissions');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [serverKey, scope, sessionId]);

  // Derive the selected session-scope option from the combined state (conv override wins).
  const sessionChoice = useCallback(
    (tool: string): Choice => {
      const conv = convMap[tool];
      if (conv) return conv === 'allow' ? 'conv_allow' : conv === 'deny' ? 'conv_deny' : 'ask';
      const glob = globalMap[tool];
      if (glob === 'allow') return 'always_allow';
      if (glob === 'deny') return 'always_deny';
      return 'ask'; // global 'ask' or nothing → effective ask
    },
    [convMap, globalMap]
  );

  const persist = useCallback(
    async (
      tool: string,
      nextGlobal: Record<string, McpVerdict>,
      nextConv: Record<string, McpVerdict>,
      scopes: Scope[]
    ) => {
      setSavingTool(tool);
      setError(null);
      try {
        if (scopes.includes('global')) await setMyMcpPolicy(serverKey, toEntries(nextGlobal));
        if (scopes.includes('session') && sessionId)
          await setSessionMcpPolicy(sessionId, serverKey, toEntries(nextConv));
        setGlobalMap(nextGlobal);
        setConvMap(nextConv);
      } catch {
        setError('Failed to save permission');
      } finally {
        setSavingTool(null);
      }
    },
    [serverKey, sessionId]
  );

  const onSessionChange = (tool: string, choice: Choice) => {
    const nextGlobal = { ...globalMap };
    const nextConv = { ...convMap };
    if (choice === 'always_allow' || choice === 'always_deny') {
      nextGlobal[tool] = choice === 'always_allow' ? 'allow' : 'deny';
      delete nextConv[tool]; // a global default must not be shadowed by a stale conv override
      void persist(tool, nextGlobal, nextConv, ['global', 'session']);
    } else {
      nextConv[tool] = choice === 'conv_allow' ? 'allow' : choice === 'conv_deny' ? 'deny' : 'ask';
      void persist(tool, nextGlobal, nextConv, ['session']);
    }
  };

  const onGlobalChange = (tool: string, verdict: McpVerdict) => {
    const nextGlobal = { ...globalMap, [tool]: verdict };
    void persist(tool, nextGlobal, convMap, ['global']);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-blue-500" />
      </div>
    );
  }

  const rows = [{ name: SERVER_DEFAULT, description: 'Default for all tools' }, ...tools];

  return (
    <div className="space-y-1">
      {error && <p className="text-xs text-red-400">{error}</p>}
      {tools.length === 0 && (
        <p className="text-xs text-gray-500">No tools enumerated for this server.</p>
      )}
      {rows.map((t) => {
        const isDefault = t.name === SERVER_DEFAULT;
        return (
          <div
            key={t.name}
            className={`flex items-center justify-between gap-3 rounded px-2 py-1.5 ${
              isDefault ? 'bg-gray-800/60' : 'hover:bg-gray-800'
            }`}
          >
            <div className="min-w-0">
              <div className="truncate text-sm text-gray-200">
                {isDefault ? 'All tools (default)' : t.name}
              </div>
              {!isDefault && t.description && (
                <div className="truncate text-xs text-gray-500" title={t.description}>
                  {t.description}
                </div>
              )}
            </div>
            {scope === 'session' ? (
              <select
                aria-label={`Permission for ${isDefault ? 'server default' : t.name}`}
                value={sessionChoice(t.name)}
                disabled={!!savingTool}
                onChange={(e) => onSessionChange(t.name, e.target.value as Choice)}
                className="shrink-0 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 disabled:opacity-50"
              >
                {SESSION_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <select
                aria-label={`Permission for ${isDefault ? 'server default' : t.name}`}
                value={globalMap[t.name] ?? 'ask'}
                disabled={!!savingTool}
                onChange={(e) => onGlobalChange(t.name, e.target.value as McpVerdict)}
                className="shrink-0 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 disabled:opacity-50"
              >
                {GLOBAL_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            )}
          </div>
        );
      })}
    </div>
  );
}
