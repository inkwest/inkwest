import { useState } from 'react';
import { Message } from '@/types/api';
import { ToolCallCard } from './ToolCallCard';
import { MarkdownContent } from './MarkdownContent';
import { useSettingsStore } from '@/store/settings';
import { useSessionsStore } from '@/store/sessions';
import { cn, TEXT_SIZE, formatTimestamp } from '@/lib/utils';
import { AlertCircle, RefreshCw, Loader2, Link2, Check } from 'lucide-react';
import * as api from '@/services/api';

interface Props {
  message: Message;
  isFirstUserMessage?: boolean;
  onRetry?: (message: Message) => void;
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
