import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, RotateCcw, Search } from 'lucide-react';
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
import { useMcpPolicyVersion } from '@/store/mcpPolicy';

// The server-wide default row is stored under tool '*'; tools inherit it unless overridden.
const SERVER_DEFAULT = '*';

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

// Three-position segmented control: allow (green) / ask (neutral) / deny (red). When `muted` —
// a conversation tool showing its INHERITED global default rather than a local override — the
// selected segment is desaturated, so a local override reads as "vibrant" by contrast.
const VERDICT_SEGMENTS: { v: McpVerdict; label: string; on: string; onMuted: string }[] = [
  { v: 'allow', label: 'Allow', on: 'bg-green-600 text-white', onMuted: 'bg-green-900/40 text-green-300' },
  { v: 'ask', label: 'Ask', on: 'bg-gray-500 text-white', onMuted: 'bg-gray-700 text-gray-400' },
  { v: 'deny', label: 'Deny', on: 'bg-red-600 text-white', onMuted: 'bg-red-900/40 text-red-300' },
];

function VerdictSlider({
  value,
  onChange,
  name,
  muted,
}: {
  value: McpVerdict;
  onChange: (v: McpVerdict) => void;
  name: string; // display name, e.g. "query"
  muted?: boolean; // inherited (not a local override) → desaturate the selected segment
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
            value === s.v
              ? muted
                ? s.onMuted
                : s.on
              : 'bg-gray-900 text-gray-400 hover:text-gray-200'
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
 * 3-position allow/ask/deny slider per tool + a cache-reload button — this is the source of truth.
 * Session scope: each tool shows its INHERITED global verdict (muted) until the user sets a LOCAL
 * override (vibrant) + a reset ↺; conversation edits never touch the global config. When the
 * global config changes elsewhere (e.g. the stacked Settings modal), the session view re-fetches
 * it so the inherited values stay accurate.
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

  // Cross-instance global-policy change signal (bumped by any global-scope write; watched by the
  // session view to refresh its inherited values).
  const bumpGlobalPolicy = useMcpPolicyVersion((s) => s.bumpGlobal);
  const globalPolicyVersion = useMcpPolicyVersion((s) => s.global);
  const seenPolicyVersion = useRef(globalPolicyVersion);

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

  // Session view: when the global policy changes elsewhere (bumped), re-fetch it so the inherited
  // (muted) verdicts reflect the new global defaults. Skips the initial value (mount already loads).
  useEffect(() => {
    if (scope !== 'session') return;
    if (seenPolicyVersion.current === globalPolicyVersion) return;
    seenPolicyVersion.current = globalPolicyVersion;
    let cancelled = false;
    getMyMcpPolicy()
      .then((r) => {
        if (!cancelled) setGlobal(toMap(r.policies, serverKey));
      })
      .catch(() => {
        /* transient; keep the last-known global */
      });
    return () => {
      cancelled = true;
    };
  }, [globalPolicyVersion, scope, serverKey]);

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

  // The verdict a conversation tool INHERITS from the user's global config: its own global row,
  // else the server default ('*'), else the system default 'ask'.
  const inherited = (tool: string): McpVerdict =>
    globalMap[tool] ?? globalMap[SERVER_DEFAULT] ?? 'ask';

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

  // Bulk: set every enumerated tool to one verdict so all sliders move at once. Global scope also
  // sets the server default ('*') so tools discovered later follow suit — that '*' is a real,
  // UI-visible + backend-resolved default. Session scope must NOT write conv '*': the conversation
  // view has no server-default control and inherits from global, so a conv '*' would silently
  // enforce on later-discovered tools while the UI shows them inheriting the global default.
  const bulkApply = (verdict: McpVerdict) => {
    const toolNames = tools.map((t) => t.name);
    setError(null);
    if (scope === 'global') {
      const next = { ...globalRef.current };
      for (const n of [SERVER_DEFAULT, ...toolNames]) next[n] = verdict;
      setGlobal(next);
      void runSave(() => setMyMcpPolicy(serverKey, toEntries(next)))
        .then(() => bumpGlobalPolicy())
        .catch(() => {
          setError('Failed to save permission');
          void resyncGlobal();
        });
    } else {
      const next = { ...convRef.current };
      for (const n of toolNames) next[n] = verdict;
      setConv(next);
      void runSave(() =>
        sessionId ? setSessionMcpPolicy(sessionId, serverKey, toEntries(next)) : Promise.resolve()
      ).catch(() => {
        setError('Failed to save permission');
        void resyncSession();
      });
    }
  };

  const onGlobalChange = (tool: string, verdict: McpVerdict) => {
    const next = { ...globalRef.current, [tool]: verdict };
    setGlobal(next); // optimistic — instant, no disable/flicker
    setError(null);
    void runSave(() => setMyMcpPolicy(serverKey, toEntries(next)))
      .then(() => bumpGlobalPolicy())
      .catch(() => {
        setError('Failed to save permission');
        void resyncGlobal();
      });
  };

  // Conversation scope: a change creates a LOCAL override (conv row); reset removes it, reverting
  // to the inherited global default. No global writes from the conversation panel.
  const saveConv = (next: VerdictMap) => {
    setConv(next);
    setError(null);
    void runSave(() =>
      sessionId ? setSessionMcpPolicy(sessionId, serverKey, toEntries(next)) : Promise.resolve()
    ).catch(() => {
      setError('Failed to save permission');
      void resyncSession();
    });
  };

  // Session only: drop every local override for this server → all tools revert to inherited.
  const bulkResetConv = () => saveConv({});

  const onConvChange = (tool: string, verdict: McpVerdict) =>
    saveConv({ ...convRef.current, [tool]: verdict });

  const onConvReset = (tool: string) => {
    if (!(tool in convRef.current)) return; // already inherited
    const next = { ...convRef.current };
    delete next[tool];
    saveConv(next);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-blue-500" />
      </div>
    );
  }

  // Per-tool control. Global scope: the plain slider (source of truth). Session scope: the same
  // slider but showing the INHERITED verdict muted until the user sets a LOCAL override (vibrant),
  // with a reset ↺ that reverts to inherited.
  const control = (name: string) => {
    if (scope === 'global') {
      return (
        <VerdictSlider
          name={name}
          value={globalMap[name] ?? 'ask'}
          onChange={(v) => onGlobalChange(name, v)}
        />
      );
    }
    const isLocal = name in convMap;
    return (
      <div className="flex shrink-0 items-center gap-1">
        <VerdictSlider
          name={name}
          value={isLocal ? convMap[name] : inherited(name)}
          muted={!isLocal}
          onChange={(v) => onConvChange(name, v)}
        />
        <button
          type="button"
          onClick={() => onConvReset(name)}
          disabled={!isLocal}
          title={isLocal ? 'Reset to your global default' : 'Inherited from your global default'}
          aria-label={`Reset ${name} to inherited`}
          className="rounded p-1 text-gray-400 hover:bg-gray-700 hover:text-gray-200 disabled:opacity-30 disabled:hover:bg-transparent"
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  };

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

      {/* Bulk actions — set every tool (and the fallback for tools added later) at once. */}
      <div className="flex items-center justify-between gap-3 rounded bg-gray-800/60 px-2 py-1.5">
        <span className="text-sm text-gray-300">All tools</span>
        <div className="flex items-center gap-3 text-xs">
          <button
            type="button"
            onClick={() => bulkApply('allow')}
            className="text-green-400 hover:underline"
          >
            Allow all
          </button>
          <button
            type="button"
            onClick={() => bulkApply('ask')}
            className="text-gray-300 hover:underline"
          >
            Ask all
          </button>
          <button
            type="button"
            onClick={() => bulkApply('deny')}
            className="text-red-400 hover:underline"
          >
            Deny all
          </button>
          {scope === 'session' && (
            <button
              type="button"
              onClick={bulkResetConv}
              title="Revert every tool to your global default"
              className="inline-flex items-center gap-1 text-gray-400 hover:underline"
            >
              <RotateCcw className="h-3 w-3" />
              Reset all
            </button>
          )}
        </div>
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
