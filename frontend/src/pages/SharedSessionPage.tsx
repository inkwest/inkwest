import { useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { useSharedSocket } from '@/hooks/useSharedSocket';
import { useChatStore } from '@/store/chat';
import { MessageList } from '@/components/chat/MessageList';
import { StreamingMessage } from '@/components/chat/StreamingMessage';
import { PresenceBar } from '@/components/chat/PresenceBar';

// Public, anonymous, read-only view of a conversation reached via a share link
// (/shared/:token). No app shell / auth — just the live transcript.
export function SharedSessionPage() {
  const { token } = useParams<{ token: string }>();
  const { status, meta, selfId } = useSharedSocket(token ?? '');
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  if (status === 'loading') {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-900 text-gray-400">
        Loading…
      </div>
    );
  }

  if (status === 'error' || status === 'revoked') {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-900 text-gray-300 gap-2">
        <h1 className="text-lg font-semibold">Inkwest</h1>
        <p className="text-sm text-gray-500">This share link is invalid or has been revoked.</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen bg-gray-900 text-white">
      <header className="h-14 border-b border-gray-800 px-4 flex items-center justify-between shrink-0">
        <div className="min-w-0">
          <div className="text-sm font-semibold truncate">
            {meta?.title || 'Shared conversation'}
          </div>
          <div className="text-xs text-gray-500 truncate">
            {meta?.owner_display_name ? `Shared by ${meta.owner_display_name} · ` : ''}read-only
          </div>
        </div>
        <span className="text-xs font-semibold text-gray-400">Inkwest</span>
      </header>

      <PresenceBar selfId={selfId} />

      <div className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0">
        <MessageList messages={messages} />
        {isStreaming && (
          <div className="mt-6">
            <StreamingMessage />
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
