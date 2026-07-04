import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ShieldQuestion } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useMcpStore, type PendingMcpAsk } from '@/store/mcp';
import { persistToolVerdict } from '@/store/mcpPolicy';

export type AskScope = 'once' | 'conversation' | 'global';

// Split-button for the ask bar: the label click decides "once"; the caret opens the scope
// menu (this conversation / always). One component so Allow/Deny stay symmetric. The menu is
// tinted to the verdict (green allow / red deny) so which action's scope list you opened reads
// at a glance.
function DecisionButton({
  label,
  solidClass,
  menuClass,
  disabled,
  // Hide "for this conversation" when the ask carries no session id (a stale backend frame) —
  // a silent no-write downgrade to "once" would mislead.
  showConversationScope,
  onDecide,
}: {
  label: string;
  solidClass: string;
  menuClass: string;
  disabled: boolean;
  showConversationScope: boolean;
  onDecide: (scope: AskScope) => void;
}) {
  const [open, setOpen] = useState(false);
  // Open downward by default (more natural); flip upward only when there isn't room below —
  // e.g. the composer-anchored fallback near the viewport bottom — so the menu never clips
  // off-screen. Measured at open time from the button's viewport position.
  const [openUp, setOpenUp] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const toggle = () => {
    if (!open && rootRef.current) {
      // ~menu height budget (w-48, up to 4 rows). Not enough room below → open upward.
      const room = window.innerHeight - rootRef.current.getBoundingClientRect().bottom;
      setOpenUp(room < 160);
    }
    setOpen((o) => !o);
  };

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
        onClick={(e) => { e.stopPropagation(); toggle(); }}
        disabled={disabled}
        aria-label={`${label} options`}
        aria-expanded={open}
        className={cn('rounded-r border-l border-black/20 px-1 py-1 text-white disabled:opacity-50', solidClass)}
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className={cn(
          'absolute right-0 z-20 w-48 overflow-hidden rounded-md border shadow-lg',
          openUp ? 'bottom-full mb-1' : 'top-full mt-1',
          menuClass,
        )}>
          {(
            [
              ['once', 'once'],
              ...(showConversationScope ? [['conversation', 'for this conversation']] : []),
              ['global', 'always (all conversations)'],
            ] as [AskScope, string][]
          ).map(([scope, text]) => (
            <button
              key={scope}
              onClick={(e) => { e.stopPropagation(); pick(scope); }}
              className="block w-full px-3 py-1.5 text-left text-xs text-gray-100 hover:bg-white/10"
            >
              {label} {text}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

/**
 * The tool-permission ask decision UI, shared verbatim between the in-bubble ToolCallCard bar
 * and the composer-anchored fallback (McpAskNotice) so both offer the SAME affordances: Allow /
 * Deny split-buttons with once / this-conversation / always scopes, each broader scope also
 * persisting the policy. `className` styles the outer row for its host (card border vs composer
 * attach). The scope menu opens UPWARD (`bottom-full`) so it's never clipped when the bar sits
 * at the bottom of the viewport above the composer.
 */
export function AskDecisionBar({ ask, className }: { ask: PendingMcpAsk; className?: string }) {
  const [deciding, setDeciding] = useState(false);

  const decide = async (verdict: 'allow' | 'deny', scope: AskScope) => {
    if (deciding) return;
    setDeciding(true);
    try {
      // Kick the broader-scope policy write off FIRST (so it lands even if the decision below
      // races the 60s hold timeout) but do NOT await it before the decision POST: the shared
      // save chain can be busy behind a slow editor PUT, and blocking on it would let the hold
      // expire into a deny the user didn't choose. Best-effort — a failed write must not block
      // the decision (a 5xx surfaces via the banner strip, a network failure via the amber
      // banner). The conversation scope targets the ASK's session, not the selected one — the
      // user may have switched sessions while the call was held.
      const persist =
        scope === 'conversation' && ask.sessionId
          ? persistToolVerdict(
              { scope: 'session', sessionId: ask.sessionId },
              ask.mcpKey,
              ask.tool,
              verdict,
            ).catch(() => undefined)
          : scope === 'global'
            ? persistToolVerdict({ scope: 'global' }, ask.mcpKey, ask.tool, verdict).catch(
                () => undefined,
              )
            : Promise.resolve();
      await useMcpStore.getState().decideAsk(ask.askId, verdict);
      await persist;
    } finally {
      setDeciding(false);
    }
  };

  return (
    <div className={cn('flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-3 py-2', className)}>
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
          menuClass="border-green-700/60 bg-green-950/95"
          disabled={deciding}
          showConversationScope={Boolean(ask.sessionId)}
          onDecide={(scope) => void decide('allow', scope)}
        />
        <DecisionButton
          label="Deny"
          solidClass="bg-red-600/80 hover:bg-red-600"
          menuClass="border-red-700/60 bg-red-950/95"
          disabled={deciding}
          showConversationScope={Boolean(ask.sessionId)}
          onDecide={(scope) => void decide('deny', scope)}
        />
      </span>
    </div>
  );
}
