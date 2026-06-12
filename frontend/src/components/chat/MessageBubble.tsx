import { useState } from 'react';
import { Message, MessageContent, SessionEventContent, SessionEventTarget } from '@/types/api';
import { ToolCallCard } from './ToolCallCard';
import { MarkdownContent } from './MarkdownContent';
import { useSettingsStore } from '@/store/settings';
import { useSessionsStore } from '@/store/sessions';
import { cn, TEXT_SIZE, formatTimestamp, formatFullTimestamp } from '@/lib/utils';
import { AlertCircle, RefreshCw, Loader2, Link2, Check } from 'lucide-react';
import * as api from '@/services/api';

interface Props {
  message: Message;
  isFirstUserMessage?: boolean;
  onRetry?: (message: Message) => void;
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

  const parts =
    typeof content === 'object' && !Array.isArray(content) && content.type === 'session_event'
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
        className={cn('italic text-gray-500', TEXT_SIZE.label(compact))}
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

export function MessageBubble({ message, isFirstUserMessage, onRetry }: Props) {
  const isUser = message.role === 'user';
  const hasError = message.status === 'error';
  const isPending = message.status === 'pending';
  const [isRetrying, setIsRetrying] = useState(false);
  const [copied, setCopied] = useState(false);
  const compact = useSettingsStore((s) => s.compactToolCards);
  const showTimestamps = useSettingsStore((s) => s.showTimestamps);
  const currentSession = useSessionsStore((s) => s.currentSession);

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

  const handleRetry = async () => {
    if (!message.is_retryable || isRetrying) return;

    setIsRetrying(true);
    try {
      const updated = await api.retryMessage(message.session_id, message.id);
      onRetry?.(updated);
    } catch (error) {
      console.error('Failed to retry message:', error);
    } finally {
      setIsRetrying(false);
    }
  };

  const handleShare = () => {
    const cluster = currentSession?.cluster;
    if (!cluster || typeof message.content !== 'string') return;

    const params = new URLSearchParams({ cluster: cluster.name, q: message.content });
    const url = `${window.location.origin}/#${params.toString()}`;
    navigator.clipboard.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const canShare = isFirstUserMessage && isUser && typeof message.content === 'string' && currentSession?.cluster;

  const renderContent = () => {
    if (typeof message.content === 'string') {
      return <MarkdownContent content={message.content} />;
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
      className={cn(
        'flex',
        isUser ? 'justify-end' : 'justify-start'
      )}
    >
      <div
        className={cn(
          'max-w-[80%] rounded-lg overflow-hidden min-w-0',
          compact ? 'px-3 py-1.5' : 'px-4 py-2',
          isUser
            ? 'bg-blue-600 text-white'
            : 'bg-gray-700 text-gray-100',
          hasError && 'border-2 border-red-500 bg-red-900/20'
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

        {renderContent()}

        {showTimestamps && message.created_at && (
          <div
            title={formatFullTimestamp(message.created_at)}
            className={cn(
              'mt-1 text-right',
              TEXT_SIZE.label(compact),
              isUser ? 'text-blue-200/60' : 'text-gray-400',
            )}
          >
            {formatTimestamp(message.created_at)}
          </div>
        )}

        {canShare && (
          <div className="flex justify-end mt-1">
            <button
              onClick={handleShare}
              className="flex items-center gap-1 text-xs text-blue-200/60 hover:text-white transition-colors"
              title="Copy shareable link"
            >
              {copied ? <Check className="w-3 h-3" /> : <Link2 className="w-3 h-3" />}
              {copied ? 'Copied!' : 'Share'}
            </button>
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

            {message.is_retryable && (
              <button
                onClick={handleRetry}
                disabled={isRetrying}
                className={cn(
                  'mt-2 flex items-center gap-1.5 rounded',
                  compact ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm',
                  'bg-red-600 hover:bg-red-700 text-white transition-colors',
                  'disabled:opacity-50 disabled:cursor-not-allowed'
                )}
              >
                {isRetrying ? (
                  <>
                    <Loader2 className={cn(compact ? 'w-3 h-3' : 'w-4 h-4', 'animate-spin')} />
                    Retrying...
                  </>
                ) : (
                  <>
                    <RefreshCw className={cn(compact ? 'w-3 h-3' : 'w-4 h-4')} />
                    Retry
                    {message.retry_count ? ` (${message.retry_count})` : ''}
                  </>
                )}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
