import { Message } from '@/types/api';
import { MessageBubble } from './MessageBubble';
import { useSettingsStore } from '@/store/settings';
import { cn } from '@/lib/utils';

interface Props {
  messages: Message[];
  // Id of the message to transiently highlight (deep-linked from a search hit).
  highlightedId?: string | null;
  // Re-run an interrupted turn from a failed message's Retry button.
  onRetry?: (messageId: string) => void;
}

export function MessageList({ messages, highlightedId, onRetry }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);
  const firstUserIdx = messages.findIndex((m) => m.role === 'user');
  // "Latest" for retry = the last actual chat turn, skipping trailing system events
  // (model change, MCP connect/disconnect) so a failed turn keeps its retry affordance
  // when a non-turn row lands after it. A genuine newer turn still supersedes it.
  let lastChatIdx = messages.length - 1;
  while (lastChatIdx >= 0 && messages[lastChatIdx].role === 'system') lastChatIdx--;

  return (
    <div className={cn(compact ? 'space-y-4' : 'space-y-6')}>
      {messages.map((message, i) => (
        <MessageBubble
          key={message.id}
          message={message}
          isFirstUserMessage={i === firstUserIdx}
          // Also match a deep-link aimed at a round fused into this bubble.
          highlighted={
            message.id === highlightedId ||
            (highlightedId != null && (message.mergedIds?.includes(highlightedId) ?? false))
          }
          isLatest={i === lastChatIdx}
          onRetry={onRetry}
        />
      ))}
    </div>
  );
}
