import { Message } from '@/types/api';
import { MessageBubble } from './MessageBubble';
import { useSettingsStore } from '@/store/settings';
import { cn } from '@/lib/utils';

interface Props {
  messages: Message[];
  // Id of the message to transiently highlight (deep-linked from a search hit).
  highlightedId?: string | null;
}

export function MessageList({ messages, highlightedId }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);
  const firstUserIdx = messages.findIndex((m) => m.role === 'user');

  return (
    <div className={cn(compact ? 'space-y-4' : 'space-y-6')}>
      {messages.map((message, i) => (
        <MessageBubble
          key={message.id}
          message={message}
          isFirstUserMessage={i === firstUserIdx}
          highlighted={message.id === highlightedId}
        />
      ))}
    </div>
  );
}
