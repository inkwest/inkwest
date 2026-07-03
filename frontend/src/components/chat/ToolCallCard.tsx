import { useState, useEffect, useRef } from 'react';
import { ChevronDown, ChevronRight, Terminal, Copy, Check, ListTodo, Circle, CheckCircle2, Loader2, ShieldQuestion } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { useSettingsStore } from '@/store/settings';
import { useMcpStore } from '@/store/mcp';
import { useSessionsStore } from '@/store/sessions';
import { useMcpPolicyVersion } from '@/store/mcpPolicy';
import {
  decideMcpAsk,
  getMyMcpPolicy,
  getSessionMcpPolicy,
  setMyMcpPolicy,
  setSessionMcpPolicy,
  type McpPolicyEntry,
  type McpVerdict,
} from '@/services/api';

// The policy PUTs are FULL-STATE per server: merge the new verdict into the server's
// current entries, else a one-tool write from the ask bar would wipe the others.
function mergePolicyEntry(
  policies: McpPolicyEntry[],
  serverKey: string,
  tool: string,
  verdict: McpVerdict,
): { tool: string; verdict: McpVerdict }[] {
  const entries = policies
    .filter((e) => e.server_key === serverKey && e.tool !== tool)
    .map((e) => ({ tool: e.tool, verdict: e.verdict }));
  entries.push({ tool, verdict });
  return entries;
}

type AskScope = 'once' | 'conversation' | 'global';

// Split-button for the ask bar: the label click decides "once"; the caret opens the
// scope menu (this conversation / always). One component so Allow/Deny stay symmetric.
function DecisionButton({
  label,
  solidClass,
  menuClass,
  disabled,
  onDecide,
}: {
  label: string;
  solidClass: string;
  menuClass: string;
  disabled: boolean;
  onDecide: (scope: AskScope) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const pick = (scope: AskScope) => {
    setOpen(false);
    onDecide(scope);
  };

  return (
    <span ref={rootRef} className="relative inline-flex">
      <button
        onClick={(e) => { e.stopPropagation(); pick('once'); }}
        disabled={disabled}
        title={`${label} this call only`}
        className={cn('rounded-l px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50', solidClass)}
      >
        {label}
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        disabled={disabled}
        aria-label={`${label} options`}
        aria-expanded={open}
        className={cn('rounded-r border-l border-black/20 px-1 py-1 text-white disabled:opacity-50', solidClass)}
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className={cn('absolute right-0 top-full z-20 mt-1 w-48 overflow-hidden rounded-md border shadow-lg', menuClass)}>
          {(
            [
              ['once', 'once'],
              ['conversation', 'for this conversation'],
              ['global', 'always (all conversations)'],
            ] as [AskScope, string][]
          ).map(([scope, text]) => (
            <button
              key={scope}
              onClick={(e) => { e.stopPropagation(); pick(scope); }}
              className="block w-full px-3 py-1.5 text-left text-xs text-gray-200 hover:bg-gray-700"
            >
              {label} {text}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  activeForm?: string;
}

interface Props {
  name: string;
  input?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  isLoading?: boolean;
  autoCollapse?: boolean;
}

export function ToolCallCard({ name, input, result, isError, isLoading, autoCollapse = true }: Props) {
  // Get compact mode from settings store
  const compact = useSettingsStore((s) => s.compactToolCards);
  // Start expanded only if loading (active tool), otherwise collapsed
  const [expanded, setExpanded] = useState(isLoading === true);
  const [copied, setCopied] = useState(false);
  const wasLoadingRef = useRef(isLoading);

  // Runtime ask gate: while this MCP call is held on the user's decision, the matching
  // pending ask renders Allow/Deny INSIDE this card (the banner is only the fallback).
  // Match on the cross-engine tool name (`mcp__<key>__<tool>`) while the call is in
  // flight; claim it so the banner (and a sibling card racing on the same tool) skips it.
  const pendingAsks = useMcpStore((s) => s.pendingAsks);
  const claimedAsks = useMcpStore((s) => s.claimedAsks);
  // MUST be a ref, not state: the claim lands in the store synchronously, so the very
  // next render already sees claimedAsks[id] — a not-yet-committed useState "mine" flag
  // would make the find() drop the ask, the cleanup release it, and the loop restart
  // (claim → release ping-pong ⇒ "Maximum update depth exceeded").
  const myClaim = useRef<string | null>(null);
  const [deciding, setDeciding] = useState(false);
  // Match while the call has NO result yet (not on isLoading: streaming re-renders can
  // drop the loading flag mid-hold, which bounced the bar back to the fallback banner).
  // A pending ask is by definition in flight, so a result-less matching card owns it.
  // Engines drift on the separator: claude-cli has been seen emitting BOTH
  // mcp__<key>__<tool> and mcp_<key>_<tool> — accept either.
  const ask = !result && !isError
    ? pendingAsks.find(
        (a) =>
          (name === `mcp__${a.mcpKey}__${a.tool}` || name === `mcp_${a.mcpKey}_${a.tool}`) &&
          (a.askId === myClaim.current || !claimedAsks[a.askId]),
      )
    : undefined;
  const askId = ask?.askId ?? null;
  useEffect(() => {
    if (!askId) return;
    myClaim.current = askId;
    useMcpStore.getState().claimAsk(askId);
    return () => {
      myClaim.current = null;
      useMcpStore.getState().releaseAsk(askId);
    };
  }, [askId]);

  const currentSessionId = useSessionsStore((s) => s.currentSession?.id);
  const decide = async (verdict: 'allow' | 'deny', scope: AskScope) => {
    if (!askId || !ask || deciding) return;
    setDeciding(true);
    try {
      // Persist the broader-scope policy FIRST so future calls skip the gate even if
      // the decision below races the 60s hold timeout. Fetch→merge→PUT because the
      // endpoint replaces the server's full entry set. Best-effort: a failed write must
      // not block resolving the held call. Bump the matching policy version so any open
      // editor (conversation popup / global Settings) re-fetches and reflects it live.
      if (scope === 'conversation' && currentSessionId) {
        await getSessionMcpPolicy(currentSessionId)
          .then((cur) =>
            setSessionMcpPolicy(
              currentSessionId,
              ask.mcpKey,
              mergePolicyEntry(cur.policies, ask.mcpKey, ask.tool, verdict),
            ),
          )
          .then(() => useMcpPolicyVersion.getState().bumpSession())
          .catch(() => undefined);
      } else if (scope === 'global') {
        await getMyMcpPolicy()
          .then((cur) =>
            setMyMcpPolicy(ask.mcpKey, mergePolicyEntry(cur.policies, ask.mcpKey, ask.tool, verdict)),
          )
          .then(() => useMcpPolicyVersion.getState().bumpGlobal())
          .catch(() => undefined);
      }
      await decideMcpAsk(askId, verdict);
    } catch {
      // 404 = already resolved/timed out; otherwise the hold times out to deny on its
      // own. Either way the prompt is dead weight now.
    } finally {
      useMcpStore.getState().removeAsk(askId);
      setDeciding(false);
    }
  };

  // Auto-expand when loading starts, auto-collapse when loading finishes or result arrives
  useEffect(() => {
    if (isLoading) {
      setExpanded(true);
      wasLoadingRef.current = true;
    } else if (autoCollapse) {
      // Collapse when loading finishes (was loading, now not) or when result arrives
      if (wasLoadingRef.current || result) {
        setExpanded(false);
        wasLoadingRef.current = false;
      }
    }
  }, [result, isLoading, autoCollapse]);

  // Extract description and command from input for Bash tools
  const description = input?.description as string | undefined;
  const command = input?.command as string | undefined;

  // Check if this is a TodoWrite tool
  const isTodoTool = name.toLowerCase().includes('todo') || Array.isArray(input?.todos);
  const todos = (input?.todos as TodoItem[] | undefined) || [];
  const completedCount = todos.filter(t => t.status === 'completed').length;
  const inProgressTodo = todos.find(t => t.status === 'in_progress');

  // Display title based on tool type
  const getDisplayTitle = () => {
    if (isTodoTool && todos.length > 0) {
      if (inProgressTodo) {
        return inProgressTodo.activeForm || inProgressTodo.content;
      }
      return `Tasks completed`;
    }
    return description || name;
  };
  const displayTitle = getDisplayTitle();

  // Todo counter badge
  const todoCounter = isTodoTool && todos.length > 0 ? `${completedCount}/${todos.length}` : null;

  // Icon sizes based on compact mode - even smaller in compact
  const iconSize = compact ? 'h-2.5 w-2.5' : 'h-4 w-4';
  const smallIconSize = compact ? 'h-2 w-2' : 'h-3 w-3';

  // Get icon based on tool type
  const getIcon = () => {
    if (isTodoTool) {
      return <ListTodo className={cn(iconSize, 'text-blue-400 shrink-0')} />;
    }
    return <Terminal className={cn(iconSize, 'text-green-400 shrink-0')} />;
  };

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const textToCopy = command || JSON.stringify(input, null, 2);
    await navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    // No overflow-hidden: it would clip the decision dropdown menu. The rounded corners
    // are applied to the first/last inner blocks instead.
    <div className={cn(
      'border rounded-md',
      compact ? 'my-1' : 'my-2',
      isError ? 'border-red-500/50' : ask ? 'border-blue-500 ring-1 ring-blue-500/50' : 'border-gray-600'
    )}>
      <button
        onClick={() => setExpanded(!expanded)}
        className={cn(
          'w-full flex items-center bg-gray-800 hover:bg-gray-700 text-left rounded-t-[5px]',
          !expanded && !ask && 'rounded-b-[5px]',
          compact ? 'gap-1.5 px-2 py-1' : 'gap-2 px-3 py-2'
        )}
      >
        {expanded ? (
          <ChevronDown className={cn(iconSize, 'shrink-0')} />
        ) : (
          <ChevronRight className={cn(iconSize, 'shrink-0')} />
        )}
        {getIcon()}
        <span className={cn('truncate flex-1', TEXT_SIZE.title(compact))}>{displayTitle}</span>
        {todoCounter && (
          <span className={cn(
            'rounded shrink-0',
            compact ? 'text-[10px] px-1 py-0.5' : 'text-xs px-1.5 py-0.5',
            completedCount === todos.length
              ? 'bg-green-900/50 text-green-400'
              : 'bg-blue-900/50 text-blue-400'
          )}>
            {todoCounter}
          </span>
        )}
        {input && !isTodoTool && (
          // span, not button: this control sits inside the header <button> and nested
          // buttons are invalid DOM (React validateDOMNesting warning).
          <span
            role="button"
            tabIndex={0}
            onClick={handleCopy}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                e.stopPropagation();
                void handleCopy(e as unknown as React.MouseEvent);
              }
            }}
            className={cn('hover:bg-gray-600 rounded shrink-0', compact ? 'p-0.5' : 'p-1')}
            title="Copy command"
          >
            {copied ? (
              <Check className={cn(smallIconSize, 'text-green-400')} />
            ) : (
              <Copy className={cn(smallIconSize, 'text-gray-400')} />
            )}
          </span>
        )}
        {isLoading && (
          <span className={cn('text-yellow-400 shrink-0', TEXT_SIZE.label(compact))}>Running...</span>
        )}
        {isError && (
          <span className={cn('text-red-400 shrink-0', TEXT_SIZE.label(compact))}>Error</span>
        )}
        {result && !isError && !isLoading && (
          <span className={cn('text-green-400 shrink-0', TEXT_SIZE.label(compact))}>Done</span>
        )}
      </button>

      {/* Held on the runtime ask gate: prominent in-bubble decision bar (60s → deny).
          "Always" persists the per-conversation tool policy, then resolves the hold. */}
      {ask && (
        <div className={cn(
          'flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-blue-600/50 bg-blue-900/40 px-3 py-2',
          !expanded && 'rounded-b-[5px]'
        )}>
          <span className="flex min-w-0 items-center gap-2 text-sm text-blue-100">
            <ShieldQuestion className="h-4 w-4 shrink-0 text-blue-300" />
            <span className="truncate">
              Allow <strong>{ask.mcpKey}</strong> to run <strong>{ask.tool}</strong>?
            </span>
          </span>
          <span className="flex shrink-0 gap-2">
            <DecisionButton
              label="Allow"
              solidClass="bg-green-600 hover:bg-green-500"
              menuClass="border-gray-600 bg-gray-800"
              disabled={deciding}
              onDecide={(scope) => void decide('allow', scope)}
            />
            <DecisionButton
              label="Deny"
              solidClass="bg-red-600/80 hover:bg-red-600"
              menuClass="border-gray-600 bg-gray-800"
              disabled={deciding}
              onDecide={(scope) => void decide('deny', scope)}
            />
          </span>
        </div>
      )}

      {expanded && (
        <div className={cn('bg-gray-900 rounded-b-[5px]', compact ? 'p-2' : 'p-3')}>
          {/* Render todo list nicely */}
          {isTodoTool && todos.length > 0 && (
            <div className="space-y-0.5">
              {todos.map((todo, idx) => (
                <div
                  key={idx}
                  className={cn(
                    'flex items-center gap-1.5 py-0.5 px-1.5 rounded',
                    TEXT_SIZE.title(compact),
                    todo.status === 'in_progress' && 'bg-blue-900/30',
                    todo.status === 'completed' && 'text-gray-500'
                  )}
                >
                  {todo.status === 'completed' && (
                    <CheckCircle2 className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-green-500 shrink-0')} />
                  )}
                  {todo.status === 'in_progress' && (
                    <Loader2 className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-blue-400 shrink-0 animate-spin')} />
                  )}
                  {todo.status === 'pending' && (
                    <Circle className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-gray-500 shrink-0')} />
                  )}
                  <span className={todo.status === 'completed' ? 'line-through' : ''}>
                    {todo.content}
                  </span>
                </div>
              ))}
            </div>
          )}
          {/* Show command prominently if it's a Bash tool */}
          {command && (
            <div className="mb-2">
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Command:</div>
              <div className="relative">
                <SyntaxHighlighter
                  language="bash"
                  style={oneDark}
                  customStyle={{
                    margin: 0,
                    padding: compact ? '0.25rem' : '0.5rem',
                    fontSize: compact ? '0.65rem' : '0.75rem',
                  }}
                >
                  {command}
                </SyntaxHighlighter>
              </div>
            </div>
          )}
          {/* Show other input fields if not a simple Bash command or todo */}
          {input && !command && !isTodoTool && (
            <div className="mb-2">
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Input:</div>
              <pre className={cn('overflow-x-auto bg-gray-800 p-2 rounded', TEXT_SIZE.label(compact))}>
                {JSON.stringify(input, null, 2)}
              </pre>
            </div>
          )}
          {result && (
            <div>
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Output:</div>
              <SyntaxHighlighter
                language="text"
                style={oneDark}
                customStyle={{
                  margin: 0,
                  padding: compact ? '0.25rem' : '0.5rem',
                  fontSize: compact ? '0.65rem' : '0.75rem',
                  maxHeight: compact ? '200px' : '300px',
                  overflow: 'auto',
                }}
              >
                {result}
              </SyntaxHighlighter>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
