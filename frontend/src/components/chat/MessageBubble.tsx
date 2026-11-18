import { useEffect, useRef, useState } from 'react';
import { Message, MessageContent, SessionEventContent, SessionEventTarget } from '@/types/api';
import { ToolCallCard } from './ToolCallCard';
import { MarkdownContent } from './MarkdownContent';
import { MessageAttachments } from './Attachments';
import { useSettingsStore } from '@/store/settings';
import { useSessionsStore } from '@/store/sessions';
import { useAuthStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { cn, TEXT_SIZE, formatTimestamp, formatFullTimestamp } from '@/lib/utils';
import { TokenGlyphs } from '@/components/ui/TokenGlyphs';
import { AlertCircle, RefreshCw, Loader2, Check, Copy, MoreHorizontal, PencilLine } from 'lucide-react';
import { useComposerStore } from '@/store/composer';
import * as api from '@/services/api';
import { engineLabel } from '@/lib/engines';
import {
  messageContentToMarkdown,
  buildExportMarkdown,
  buildExportHtml,
  buildExportHtmlTeams,
  copyRich,
  copyToClipboard,
  type ExportFooterMeta,
} from '@/lib/messageExport';

interface Props {
  message: Message;
  onRetry?: (messageId: string) => void;
  // Transiently emphasized when deep-linked from a search hit (see ChatView).
  highlighted?: boolean;
  // Whether this is the last message in the stream. Retry is only offered on the
  // latest turn; an older failure is shown darkened with no retry (replaying a
  // superseded turn would rewrite history from the wrong point). Defaults to true
  // so a bubble rendered outside a list keeps its retry affordance.
  isLatest?: boolean;
}

// Display labels for session-event subjects; falls back to the raw key so a new
// subject still renders sensibly without a change here.
const SUBJECT_LABELS: Record<string, string> = { aws: 'AWS', gitlab: 'GitLab', mcp: 'MCP' };

// "a, b and c" — Oxford-less natural join for the target list.
function joinNatural(items: string[]): string {
  const xs = items.filter(Boolean);
  if (xs.length <= 1) return xs[0] ?? '';
  return `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;
}

// Display the human account name and role only — the account id stays in the stored
// `targets` structure (full detail for export/audit) but is kept out of the line.
function awsTargetLabel(t: SessionEventTarget): string {
  const acct = t.account_name || t.account_id || '';
  return t.role ? `${acct}/${t.role}` : acct;
}

// Compose the inline wording from the structured event into a `lead` phrase plus the
// list of target labels (kept separate so long lists can truncate with an expander).
// Subject-specific phrasing lives here; unknown subjects degrade gracefully.
function sessionEventParts(c: SessionEventContent): { lead: string; items: string[] } {
  const targets = c.targets ?? [];
  switch (c.subject) {
    case 'aws': {
      const items = targets.map(awsTargetLabel).filter(Boolean);
      return { lead: items.length ? `AWS access ${c.action} to:` : `AWS access ${c.action}`, items };
    }
    case 'gitlab':
      return { lead: `GitLab access ${c.action}`, items: [] };
    case 'mcp': {
      const items = targets.map((t) => t.server ?? '').filter(Boolean);
      const prep = c.action === 'connected' ? 'to' : 'from';
      return { lead: items.length ? `MCP ${c.action} ${prep}:` : `MCP ${c.action}`, items };
    }
    case 'model': {
      const model = targets[0]?.model;
      return { lead: model ? `Model changed to ${model}` : 'Model reset to the default', items: [] };
    }
    case 'turn': {
      // Abort marker (turn_runner): why a turn ended without a normal finish, so a
      // reloaded/shared view doesn't blame the turn's last tool call.
      const reason = targets[0]?.reason;
      return { lead: reason ? `Turn aborted — ${reason}` : 'Turn aborted', items: [] };
    }
    default:
      return { lead: `${SUBJECT_LABELS[c.subject] ?? c.subject} ${c.action}`, items: [] };
  }
}

// Beyond this many targets the list collapses to "first N … and M more" with a
// click-to-expand, so a grant over dozens of accounts stays one readable line.
const MAX_INLINE_TARGETS = 5;

// One inline system-event line (faded, timestamped). Owns the expand state for long
// target lists, so it must be its own component (hooks can't live behind the parent's
// early return).
function SessionEventLine({
  content,
  createdAt,
  compact,
  showTimestamps,
}: {
  content: MessageContent;
  createdAt: string;
  compact: boolean;
  showTimestamps: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  const isEvent =
    typeof content === 'object' && !Array.isArray(content) && content.type === 'session_event';
  // Abort markers read as failures, not housekeeping — tint them so a viewer scanning
  // a shared transcript can't miss why the turn above ends mid-tool-call.
  const isAbort = isEvent && content.subject === 'turn';
  const parts = isEvent
    ? sessionEventParts(content)
    : typeof content === 'string'
      ? { lead: content, items: [] }
      : null;
  if (!parts || (!parts.lead && parts.items.length === 0)) return null;

  const { lead, items } = parts;
  const collapsible = items.length > MAX_INLINE_TARGETS;
  const truncated = collapsible && !expanded;
  const shown = truncated ? items.slice(0, MAX_INLINE_TARGETS) : items;

  const toggle = (label: string) => (
    <button
      type="button"
      onClick={() => setExpanded((v) => !v)}
      className="underline decoration-dotted hover:text-gray-300"
    >
      {label}
    </button>
  );

  return (
    <div className="flex justify-center">
      <span
        title={showTimestamps ? formatFullTimestamp(createdAt) : undefined}
        className={cn('italic', isAbort ? 'text-red-400/90' : 'text-gray-500', TEXT_SIZE.label(compact))}
      >
        {showTimestamps && (
          <>
            <span className="not-italic tabular-nums opacity-80">
              {formatTimestamp(createdAt)}
            </span>
            {' · '}
          </>
        )}
        {lead}
        {items.length > 0 && ' '}
        {truncated ? (
          <>
            {shown.join(', ')} and {toggle(`${items.length - MAX_INLINE_TARGETS} more`)}
          </>
        ) : (
          <>
            {joinNatural(shown)}
            {collapsible && <> {toggle('show less')}</>}
          </>
        )}
      </span>
    </div>
  );
}

/** Per-turn token line for assistant bubbles (delegates the glyph rendering to the
 *  shared TokenGlyphs). Renders nothing when the row predates the token columns
 *  (all four fields null) — a partially-null row (odd data) shows zeros rather than
 *  hiding a non-zero cache count. */
function TokenCountsLine({ message, compact }: { message: Message; compact: boolean }) {
  const input = message.input_tokens ?? null;
  const output = message.output_tokens ?? null;
  const cacheRead = message.cache_read_tokens ?? 0;
  const cacheWrite = message.cache_write_tokens ?? 0;
  if (input === null && output === null && !cacheRead && !cacheWrite) return null;
  return (
    <TokenGlyphs
      className={cn('font-mono text-gray-400', TEXT_SIZE.label(compact))}
      input={input ?? 0}
      output={output ?? 0}
      cacheRead={cacheRead}
      cacheWrite={cacheWrite}
    />
  );
}

export function MessageBubble({ message, onRetry, highlighted, isLatest = true }: Props) {
  const isUser = message.role === 'user';
  const hasError = message.status === 'error';
  // Only the latest failure is replayable; older ones just dim (no retry button).
  const showRetry = hasError && message.is_retryable && isLatest;
  const isPending = message.status === 'pending';
  const [copiedKind, setCopiedKind] = useState<'rich' | 'teams' | 'md' | null>(null);
  const [copyMenuOpen, setCopyMenuOpen] = useState(false);
  const copyMenuRef = useRef<HTMLDivElement>(null);
  const compact = useSettingsStore((s) => s.compactToolCards);
  const showTimestamps = useSettingsStore((s) => s.showTimestamps);
  const showTokenCounts = useSettingsStore((s) => s.showTokenCounts);
  // Same guard as TokenCountsLine's own null-row check, hoisted so the footer div
  // doesn't render as an empty spacer on rows predating the token columns.
  const hasTokenCounts =
    showTokenCounts &&
    message.role === 'assistant' &&
    (message.input_tokens != null ||
      message.output_tokens != null ||
      (message.cache_read_tokens ?? 0) > 0 ||
      (message.cache_write_tokens ?? 0) > 0);
  const currentSession = useSessionsStore((s) => s.currentSession);
  const user = useAuthStore((s) => s.user);
  const config = useConfigStore((s) => s.config);

  // Close the copy menu on an outside click or Escape. Declared before any early
  // return so the hook order stays stable across renders (rules of hooks).
  useEffect(() => {
    if (!copyMenuOpen) return;
    const onPointer = (e: MouseEvent) => {
      if (copyMenuRef.current && !copyMenuRef.current.contains(e.target as Node)) setCopyMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      // Consume Escape here so the topmost overlay wins — otherwise it bubbles to the
      // global Escape-to-stop listener (ChatInput) and would also abort a live stream.
      if (e.key === 'Escape') {
        e.stopPropagation();
        setCopyMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [copyMenuOpen]);

  // System events (e.g. connector access changes) are not chat turns: render them as a
  // centered, faded, italic note in the stream rather than a user/assistant bubble.
  if (message.role === 'system') {
    return (
      <SessionEventLine
        content={message.content}
        createdAt={message.created_at}
        compact={compact}
        showTimestamps={showTimestamps}
      />
    );
  }

  // Fire-and-forget over the chat WS: the backend replays the interrupted turn in
  // place and onRetry optimistically clears this bubble's error UI, so there's no
  // local pending state to track here.
  const handleRetry = () => {
    if (!message.is_retryable) return;
    onRetry?.(message.id);
  };

  // "Reuse prompt": load a past user prompt back into the composer to edit & resend.
  // String content only; the raw text (including internal newlines) is injected verbatim.
  const canReusePrompt = isUser && typeof message.content === 'string' && message.content.trim().length > 0;
  const handleReusePrompt = () => {
    if (typeof message.content === 'string') useComposerStore.getState().inject(message.content);
  };

  // Copy-as-Markdown is offered on assistant answers only, and only when there is
  // actual text to copy (a tool-only turn has no Markdown body).
  const markdownBody = message.role === 'assistant' ? messageContentToMarkdown(message.content) : '';
  const canCopyMarkdown = markdownBody.trim().length > 0;

  const exportMeta = (): ExportFooterMeta => {
    // Prefer the model stored on the turn itself (exact); fall back to the session's
    // currently-resolved choice for older messages saved before per-turn model capture.
    const modelId = message.model ?? currentSession?.model ?? config?.model;
    // Prefer the engine captured on the turn itself (exact); fall back to the session's
    // current pin, then the deployment default, for messages saved before per-turn capture.
    const engineId =
      message.engine ?? currentSession?.engine ?? config?.default_engine ?? null;
    return {
      conversationName: user?.display_name,
      model: modelId && config ? api.modelLabel(config.available_models, modelId) : modelId ?? null,
      engine: engineId ? engineLabel(engineId) : null,
      date: message.created_at,
      sessionId: message.session_id,
      messageId: message.id,
    };
  };

  const flashCopied = (kind: 'rich' | 'teams' | 'md') => {
    setCopiedKind(kind);
    setTimeout(() => setCopiedKind(null), 2000);
  };

  // Primary: rich text + Markdown fallback in one write — paste keeps formatting in
  // Docs/Outlook/Slack, or lands as raw Markdown in a plain editor.
  const handleCopyRich = async () => {
    const meta = exportMeta();
    const html = await buildExportHtml(markdownBody, meta);
    if (await copyRich(html, buildExportMarkdown(markdownBody, meta))) {
      flashCopied('rich');
    }
  };

  // Teams-specific rich variant (no <pre>, monospace via <code>, <br> line breaks).
  const handleCopyTeams = async () => {
    setCopyMenuOpen(false);
    const meta = exportMeta();
    const html = await buildExportHtmlTeams(markdownBody, meta);
    if (await copyRich(html, buildExportMarkdown(markdownBody, meta))) {
      flashCopied('teams');
    }
  };

  // Raw Markdown only (e.g. pasting the source into a rich editor).
  const handleCopyMarkdown = async () => {
    setCopyMenuOpen(false);
    if (await copyToClipboard(buildExportMarkdown(markdownBody, exportMeta()))) {
      flashCopied('md');
    }
  };

  const renderContent = () => {
    if (typeof message.content === 'string') {
      return <MarkdownContent content={message.content} breaks={isUser} />;
    }
    // Only block-list content remains here (system-event objects return early above).
    if (!Array.isArray(message.content)) return null;

    return message.content.map((block, index) => {
      if (block.type === 'text') {
        return <MarkdownContent key={index} content={block.text || ''} />;
      }
      if (block.type === 'tool_use') {
        return (
          <ToolCallCard
            key={index}
            name={block.name!}
            input={block.input!}
            autoCollapse={false}
          />
        );
      }
      if (block.type === 'tool_result') {
        return (
          <ToolCallCard
            key={index}
            name="Result"
            result={block.content}
            isError={block.is_error}
            autoCollapse={false}
          />
        );
      }
      return null;
    });
  };

  return (
    <div
      id={`message-${message.id}`}
      className={cn(
        'flex',
        isUser ? 'justify-end' : 'justify-start'
      )}
    >
      {/* Non-clipping wrapper so the copy menu can overflow the bubble (which is
          overflow-hidden to clip content to its rounded corners). */}
      <div
        className={cn(
          'group relative max-w-[80%] min-w-0',
          // Superseded failure (not the latest turn): dim the whole bubble incl. the
          // overflowing copy menu, but not while a search deep-link highlights it.
          hasError && !isLatest && !highlighted && 'opacity-50 transition-opacity',
        )}
      >
        {canCopyMarkdown && (
          <div
            ref={copyMenuRef}
            className={cn(
              'absolute top-1 right-1 z-20 flex items-center gap-0.5',
              // Keep visible while the menu is open, even if the pointer leaves the bubble.
              copyMenuOpen
                ? 'opacity-100'
                : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100',
              'transition-opacity',
            )}
          >
            <button
              onClick={handleCopyRich}
              aria-label="Copy formatted"
              title="Copy — formatted (paste into Docs, Outlook, Slack)"
              className="rounded p-1 text-gray-300 bg-gray-700/80 hover:bg-gray-600 hover:text-white"
            >
              {copiedKind ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
            <div className="relative">
              <button
                onClick={() => setCopyMenuOpen((o) => !o)}
                aria-label="More copy options"
                aria-haspopup="menu"
                aria-expanded={copyMenuOpen}
                title="More copy options"
                className="rounded p-1 text-gray-300 bg-gray-700/80 hover:bg-gray-600 hover:text-white"
              >
                <MoreHorizontal className="w-3.5 h-3.5" />
              </button>
              {copyMenuOpen && (
                <div
                  role="menu"
                  className="absolute right-0 top-full mt-1 min-w-[150px] overflow-hidden rounded-md border border-gray-600 bg-gray-800 py-1 shadow-lg"
                >
                  <button
                    role="menuitem"
                    onClick={handleCopyTeams}
                    className="block w-full px-3 py-1.5 text-left text-xs text-gray-200 hover:bg-gray-700"
                  >
                    Copy for Teams
                  </button>
                  <button
                    role="menuitem"
                    onClick={handleCopyMarkdown}
                    className="block w-full px-3 py-1.5 text-left text-xs text-gray-200 hover:bg-gray-700"
                  >
                    Copy as Markdown
                  </button>
                </div>
              )}
            </div>
          </div>
        )}

        <div
          className={cn(
            'rounded-lg overflow-hidden transition-shadow duration-700',
            compact ? 'px-3 py-1.5' : 'px-4 py-2',
            isUser ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-100',
            hasError && 'border-2 border-red-500 bg-red-900/20',
            // Deep-linked from search: fade in a ring, then it transitions out when the
            // highlight flag clears (~1.5s later, in ChatView).
            highlighted && 'ring-2 ring-amber-400',
          )}
        >
        {isPending && (
          <div className={cn(
            'flex items-center gap-2 text-yellow-400 mb-2',
            TEXT_SIZE.label(compact)
          )}>
            <Loader2 className={cn(compact ? 'w-3 h-3' : 'w-4 h-4', 'animate-spin')} />
            <span>Processing...</span>
          </div>
        )}

        {/* Zero-size scroll anchors for rounds fused into this bubble, so a
            search deep-link to an absorbed row id still resolves (see MessageList). */}
        {message.mergedIds?.map((mid) => (
          <span key={mid} id={`message-${mid}`} aria-hidden className="block h-0 w-0 overflow-hidden" />
        ))}

        {renderContent()}

        {isUser && message.attachments && message.attachments.length > 0 && (
          <MessageAttachments sessionId={message.session_id} attachments={message.attachments} />
        )}

        {((showTimestamps && message.created_at) || canReusePrompt || hasTokenCounts) && (
          <div className={cn('mt-1 flex items-center justify-end gap-2', TEXT_SIZE.label(compact))}>
            {hasTokenCounts && <TokenCountsLine message={message} compact={compact} />}
            {canReusePrompt && (
              <button
                type="button"
                onClick={handleReusePrompt}
                title="Edit & resend — load this prompt into the composer"
                className="flex items-center gap-1 text-blue-200/60 opacity-0 transition group-hover:opacity-100 focus-within:opacity-100 hover:text-white"
              >
                <PencilLine className="w-3 h-3" />
                Edit
              </button>
            )}
            {showTimestamps && message.created_at && (
              <span
                title={formatFullTimestamp(message.created_at)}
                className={isUser ? 'text-blue-200/60' : 'text-gray-400'}
              >
                {formatTimestamp(message.created_at)}
              </span>
            )}
          </div>
        )}

        {hasError && (
          <div className={cn('border-t border-red-500/30', compact ? 'mt-2 pt-2' : 'mt-3 pt-3')}>
            <div className={cn('flex items-start gap-2 text-red-400', TEXT_SIZE.label(compact))}>
              <AlertCircle className={cn(compact ? 'w-3 h-3' : 'w-4 h-4', 'mt-0.5 shrink-0')} />
              <div className="flex-1">
                <p className="font-medium">Failed to send</p>
                {message.error_message && (
                  <p className={cn('text-red-300 mt-1 break-words', TEXT_SIZE.label(compact))}>
                    {message.error_message}
                  </p>
                )}
              </div>
            </div>

            {showRetry && (
              <button
                onClick={handleRetry}
                className={cn(
                  'mt-2 flex items-center gap-1.5 rounded',
                  compact ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm',
                  'bg-red-600 hover:bg-red-700 text-white transition-colors'
                )}
              >
                <RefreshCw className={cn(compact ? 'w-3 h-3' : 'w-4 h-4')} />
                Retry
                {message.retry_count ? ` (${message.retry_count})` : ''}
              </button>
            )}
          </div>
        )}
        </div>
      </div>
    </div>
  );
}
