import { useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { useSharedSocket } from '@/hooks/useSharedSocket';
import { useChatStore } from '@/store/chat';
import { MessageList } from '@/components/chat/MessageList';
import { StreamingMessage } from '@/components/chat/StreamingMessage';

// Slack-style tail-follow threshold: within this many px of the bottom counts as
// "reading the tail", so new content keeps scrolling into view.
const NEAR_BOTTOM_PX = 120;

// Public, anonymous, read-only view of a conversation reached via a share link
// (/shared/:token). No app shell / auth — just the live transcript. Presence is
// intentionally omitted here: a link recipient shouldn't see who else is viewing
// (the owner still sees guests in the authenticated view).
//
// Being outside the shell means the global BackendBanner never renders here, so
// this page carries its own connectivity line — a guest must be able to tell a
// finished conversation from a view that stopped receiving updates.
export function SharedSessionPage() {
  const { token } = useParams<{ token: string }>();
  const { status, connection, meta } = useSharedSocket(token ?? '');
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Sampled on scroll, i.e. BEFORE new content lands: measuring inside the effect
  // below would see the grown scrollHeight and read a guest at the tail as "away"
  // whenever the appended turn is taller than the threshold.
  const atBottomRef = useRef(true);
  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  };

  useEffect(() => {
    // A reconnect re-reads the whole transcript, so this fires on every reconnect
    // too: follow the tail only if the guest is already there.
    if (!atBottomRef.current) return;
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  if (status === 'loading') {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-900 text-gray-400">
        Loading…
      </div>
    );
  }

  // Revoked is terminal and about the LINK; a transient backend failure is neither,
  // and telling an external viewer their valid link is dead is its own small outage.
  if (status === 'revoked') {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-900 text-gray-300 gap-2">
        <h1 className="text-lg font-semibold">Inkwest</h1>
        <p className="text-sm text-gray-500">This share link is invalid or has been revoked.</p>
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-900 text-gray-300 gap-2">
        <h1 className="text-lg font-semibold">Inkwest</h1>
        <p className="text-sm text-gray-500">
          {connection === 'stale'
            ? "Can't load this conversation right now."
            : 'Loading this conversation is taking longer than usual — retrying…'}
        </p>
        <p className="text-xs text-gray-600">The link itself is fine.</p>
        {connection === 'stale' && <ReloadLink label="Try again" />}
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
        <div className="flex items-center gap-3 shrink-0">
          {connection === 'reconnecting' && (
            <span className="text-xs text-amber-400" role="status">
              Reconnecting…
            </span>
          )}
          {connection === 'stale' && <ReloadLink label="Disconnected — reload for the latest" />}
          <span className="text-xs font-semibold text-gray-400">Inkwest</span>
        </div>
      </header>

      <div onScroll={onScroll} className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0">
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

function ReloadLink({ label }: { label: string }) {
  return (
    <button
      type="button"
      onClick={() => window.location.reload()}
      className="text-xs text-red-400 underline underline-offset-2 hover:text-red-300"
    >
      {label}
    </button>
  );
}
