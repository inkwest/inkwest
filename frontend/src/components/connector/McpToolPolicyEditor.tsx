import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';
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

type Scope = 'global' | 'session';

interface Props {
  serverKey: string;
  scope: Scope;
  // Required when scope === 'session'.
  sessionId?: string;
}

type VerdictMap = Record<string, McpVerdict>;

function toMap(entries: McpPolicyEntry[], serverKey: string): VerdictMap {
  const m: VerdictMap = {};
  for (const e of entries) if (e.server_key === serverKey) m[e.tool] = e.verdict;
  return m;
}

function toEntries(map: VerdictMap): { tool: string; verdict: McpVerdict }[] {
  return Object.entries(map).map(([tool, verdict]) => ({ tool, verdict }));
}

// Three-position segmented control: allow (green) / ask (neutral) / deny (red).
const VERDICT_SEGMENTS: { v: McpVerdict; label: string; on: string }[] = [
  { v: 'allow', label: 'Allow', on: 'bg-green-700 text-white' },
  { v: 'ask', label: 'Ask', on: 'bg-gray-500 text-white' },
  { v: 'deny', label: 'Deny', on: 'bg-red-700 text-white' },
];

function VerdictSlider({
  value,
  onChange,
  name,
}: {
  value: McpVerdict;
  onChange: (v: McpVerdict) => void;
  name: string; // display name, e.g. "query" or "server default"
}) {
  return (
    <div
      role="group"
      aria-label={`Permission for ${name}`}
      className="inline-flex shrink-0 overflow-hidden rounded border border-gray-700"
    >
      {VERDICT_SEGMENTS.map((s) => (
        <button
          key={s.v}
          type="button"
          aria-label={`${s.label} ${name}`}
          aria-pressed={value === s.v}
          onClick={() => onChange(s.v)}
          className={`px-2.5 py-1 text-xs transition-colors ${
            value === s.v ? s.on : 'bg-gray-900 text-gray-400 hover:text-gray-200'
          }`}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

// Case-insensitive highlight of the query within text (returns React nodes).
function highlight(text: string, query: string) {
  if (!query) return text;
  const q = query.toLowerCase();
  const out: (string | JSX.Element)[] = [];
  let i = 0;
  let n = 0;
  const lower = text.toLowerCase();
  while (i < text.length) {
    const hit = lower.indexOf(q, i);
    if (hit === -1) {
      out.push(text.slice(i));
      break;
    }
    if (hit > i) out.push(text.slice(i, hit));
    out.push(
      <mark key={n++} className="rounded-sm bg-yellow-600/40 text-inherit">
        {text.slice(hit, hit + q.length)}
      </mark>
    );
    i = hit + q.length;
  }
  return out;
}

/**
 * Per-tool permission editor for one MCP server. Global scope: searchable flat tool list with a
 * 3-position allow/ask/deny slider per tool + a cache-reload button. Session scope: the 5-option
 * dropdown (always/this-conversation × allow/deny, plus ask); a "global" option clears the
 * conversation override so it can't shadow the new default.
 *
 * Verdict changes are OPTIMISTIC (state updates instantly, the PUT runs in the background) so a
 * click never disables/flickers the list; the latest maps live in refs so rapid edits across
 * tools can't lose an update through the per-server full-state PUT. A failed save resyncs from
 * the server.
 */
export function McpToolPolicyEditor({ serverKey, scope, sessionId }: Props) {
  const [tools, setTools] = useState<McpToolInfo[]>([]);
  const [globalMap, setGlobalMap] = useState<VerdictMap>({});
  const [convMap, setConvMap] = useState<VerdictMap>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);
  const [query, setQuery] = useState('');

  // Latest maps, read synchronously by change handlers (state commits are async).
  const globalRef = useRef<VerdictMap>({});
  const convRef = useRef<VerdictMap>({});
  // Serialize per-server full-state PUTs so out-of-order delivery can't regress the server to
  // an older (subset) snapshot — the last enqueued save (latest state) always lands last.
  const saveChain = useRef<Promise<unknown>>(Promise.resolve());
  const runSave = (fn: () => Promise<unknown>): Promise<unknown> => {
    const p = saveChain.current.then(fn, fn); // run in order, regardless of prior outcome
    saveChain.current = p.catch(() => {}); // a rejection must not break the chain
    return p;
  };
  const setGlobal = (m: VerdictMap) => {
    globalRef.current = m;
    setGlobalMap(m);
  };
  const setConv = (m: VerdictMap) => {
    convRef.current = m;
    setConvMap(m);
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load = async () => {
      // Settle independently: a failed tool enumeration must not blank the editor — the policy
      // still loads and the server-default control still renders.
      const [toolsRes, globalRes, convRes] = await Promise.allSettled([
        getMcpServerTools(serverKey),
        getMyMcpPolicy(),
        scope === 'session' && sessionId
          ? getSessionMcpPolicy(sessionId)
          : Promise.resolve({ policies: [] as McpPolicyEntry[] }),
      ]);
      if (cancelled) return;
      if (toolsRes.status === 'fulfilled') {
        setTools(toolsRes.value.tools);
        setFetchedAt(toolsRes.value.fetched_at);
      } else {
        setError("Couldn't list this server's tools (it may be unreachable or not connected).");
      }
      if (globalRes.status === 'fulfilled') setGlobal(toMap(globalRes.value.policies, serverKey));
      if (convRes.status === 'fulfilled') setConv(toMap(convRes.value.policies, serverKey));
      setLoading(false);
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [serverKey, scope, sessionId]);

  const reload = useCallback(async () => {
    setReloading(true);
    setError(null);
    try {
      const res = await getMcpServerTools(serverKey, true); // force re-enumeration
      setTools(res.tools);
      setFetchedAt(res.fetched_at);
    } catch {
      setError("Couldn't refresh this server's tools (it may be unreachable or not connected).");
    } finally {
      setReloading(false);
    }
  }, [serverKey]);

  const sessionChoice = (tool: string): Choice => {
    const conv = convMap[tool];
    if (conv) return conv === 'allow' ? 'conv_allow' : conv === 'deny' ? 'conv_deny' : 'ask';
    const glob = globalMap[tool];
    if (glob === 'allow') return 'always_allow';
    if (glob === 'deny') return 'always_deny';
    return 'ask';
  };

  // Resync a scope from the server after a failed save (drop the optimistic state for truth).
  const resyncGlobal = async () => {
    try {
      const r = await getMyMcpPolicy();
      setGlobal(toMap(r.policies, serverKey));
    } catch {
      /* leave optimistic state; the error notice already tells the user to retry */
    }
  };
  const resyncSession = async () => {
    if (!sessionId) return;
    try {
      const r = await getSessionMcpPolicy(sessionId);
      setConv(toMap(r.policies, serverKey));
    } catch {
      /* as above */
    }
  };

  const onGlobalChange = (tool: string, verdict: McpVerdict) => {
    const next = { ...globalRef.current, [tool]: verdict };
    setGlobal(next); // optimistic — instant, no disable/flicker
    setError(null);
    void runSave(() => setMyMcpPolicy(serverKey, toEntries(next))).catch(() => {
      setError('Failed to save permission');
      void resyncGlobal();
    });
  };

  const onSessionChange = (tool: string, choice: Choice) => {
    const nextGlobal = { ...globalRef.current };
    const nextConv = { ...convRef.current };
    const touchesGlobal = choice === 'always_allow' || choice === 'always_deny';
    if (touchesGlobal) {
      nextGlobal[tool] = choice === 'always_allow' ? 'allow' : 'deny';
      delete nextConv[tool]; // a global default must not be shadowed by a stale conv override
      setGlobal(nextGlobal);
      setConv(nextConv);
    } else {
      nextConv[tool] = choice === 'conv_allow' ? 'allow' : choice === 'conv_deny' ? 'deny' : 'ask';
      setConv(nextConv);
    }
    setError(null);
    void runSave(() => {
      const puts: Promise<unknown>[] = [];
      if (touchesGlobal) puts.push(setMyMcpPolicy(serverKey, toEntries(nextGlobal)));
      if (sessionId) puts.push(setSessionMcpPolicy(sessionId, serverKey, toEntries(nextConv)));
      return Promise.all(puts);
    }).catch(() => {
      setError('Failed to save permission');
      if (touchesGlobal) void resyncGlobal();
      void resyncSession();
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-blue-500" />
      </div>
    );
  }

  const control = (name: string) =>
    scope === 'session' ? (
      <select
        aria-label={`Permission for ${name === SERVER_DEFAULT ? 'server default' : name}`}
        value={sessionChoice(name)}
        onChange={(e) => onSessionChange(name, e.target.value as Choice)}
        className="shrink-0 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200"
      >
        {SESSION_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    ) : (
      <VerdictSlider
        name={name === SERVER_DEFAULT ? 'server default' : name}
        value={globalMap[name] ?? 'ask'}
        onChange={(v) => onGlobalChange(name, v)}
      />
    );

  const q = query.trim().toLowerCase();
  const filtered = q
    ? tools.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          (t.annotations?.title || '').toLowerCase().includes(q) ||
          t.description.toLowerCase().includes(q)
      )
    : tools;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tools…"
            aria-label="Search tools"
            className="w-full rounded border border-gray-700 bg-gray-900 py-1 pl-7 pr-2 text-xs text-gray-200 placeholder-gray-500"
          />
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={reloading}
          className="inline-flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"
          title="Re-enumerate this server's tools"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${reloading ? 'animate-spin' : ''}`} />
          Reload
        </button>
      </div>

      <div className="text-xs text-gray-500">
        {fetchedAt ? `Tools cached ${new Date(fetchedAt).toLocaleString()}` : 'Tools not yet cached'}
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}

      {/* Server-wide default — applies to any tool without its own verdict. */}
      <div className="flex items-center justify-between gap-3 rounded bg-gray-800/60 px-2 py-1.5">
        <div className="text-sm text-gray-200">All tools (default)</div>
        {control(SERVER_DEFAULT)}
      </div>

      {tools.length === 0 ? (
        !error && <p className="text-xs text-gray-500">No tools enumerated for this server.</p>
      ) : filtered.length === 0 ? (
        <p className="text-xs text-gray-500">No tools match “{query}”.</p>
      ) : (
        <div className="space-y-0.5">
          {filtered.map((t) => (
            <div
              key={t.name}
              className="flex items-center justify-between gap-3 rounded px-2 py-1.5 hover:bg-gray-800"
            >
              <div className="min-w-0">
                <div className="truncate text-sm text-gray-200">
                  {highlight(t.annotations?.title || t.name, query)}
                </div>
                {t.description && (
                  // While searching, drop the truncation so a match inside the description is
                  // actually visible (highlighted); otherwise keep it to one tidy line.
                  <div
                    className={
                      q
                        ? 'whitespace-pre-wrap break-words text-xs text-gray-500'
                        : 'truncate text-xs text-gray-500'
                    }
                    title={t.description}
                  >
                    {highlight(t.description, query)}
                  </div>
                )}
              </div>
              {control(t.name)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
