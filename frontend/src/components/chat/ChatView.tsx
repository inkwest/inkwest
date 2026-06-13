import { useEffect, useRef, useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useChatStore } from '@/store/chat';
import { useSettingsStore } from '@/store/settings';
import { useEgressSocket, isBlocked } from '@/hooks/useEgressSocket';
import { MessageList } from './MessageList';
import { ChatInput } from './ChatInput';
import { StreamingMessage } from './StreamingMessage';
import { ErrorBanner } from './ErrorBanner';
import { FloatingTodoPanel } from './FloatingTodoPanel';
import { EgressLog } from './EgressPanel';
import { ConnectorPanel } from '@/components/connector/ConnectorPanel';
import { ConnectorInlineNotice } from './ConnectorInlineNotice';
import { cn } from '@/lib/utils';

interface Props {
  onSendMessage: (content: string) => void;
  onStopStreaming?: () => void;
  initialPrompt?: string | null;
  // Auto-submit initialPrompt once the composer is enabled (WS connected).
  autoSubmit?: boolean;
  sessionId?: string;
}

export function ChatView({ onSendMessage, onStopStreaming, initialPrompt, autoSubmit, sessionId }: Props) {
  const { messages, streamingMessage, isStreaming, isConnected, streamingBlocks, currentTodos } = useChatStore();
  const compact = useSettingsStore((s) => s.compactToolCards);
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);

  // Deep-link from a search hit: `?m=<messageId>` scrolls that message into view and
  // highlights it briefly. The param is consumed (cleared) once handled so new messages
  // or re-renders don't re-trigger the jump.
  const [searchParams, setSearchParams] = useSearchParams();
  const targetMessageId = searchParams.get('m');
  const [highlightId, setHighlightId] = useState<string | null>(null);

  // Live network-egress audit (sandbox only). The WS stays subscribed regardless of the
  // active tab so the counter keeps updating while the user is on the Conversation tab.
  const egress = useEgressSocket(sessionId);
  const [tab, setTab] = useState<'chat' | 'egress'>('chat');
  const egressAvailable = egress.connected || egress.events.length > 0;
  const blockedCount = egress.events.filter(isBlocked).length;
  const showEgress = egressAvailable && tab === 'egress';

  // Check if scrolled to bottom (with 50px threshold)
  const checkIfAtBottom = useCallback(() => {
    const container = containerRef.current;
    if (!container) return true;
    const threshold = 50;
    return container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
  }, []);

  // Handle scroll events to track position
  const handleScroll = useCallback(() => {
    isAtBottomRef.current = checkIfAtBottom();
  }, [checkIfAtBottom]);

  // Scroll the message viewport (and only it) to the bottom. We set the container's own
  // scrollTop instead of calling bottomRef.scrollIntoView() because scrollIntoView walks
  // up and scrolls *every* scrollable ancestor — with the composer's auto-grow firing a
  // resize on each keystroke, that yanked the whole chat panel into an outer scrollport.
  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const c = containerRef.current;
    if (c) c.scrollTo({ top: c.scrollHeight, behavior });
  }, []);

  // Auto-scroll only if user was at bottom — but never while a deep-link jump is pending
  // (it would fight the scroll-to-message below).
  useEffect(() => {
    if (targetMessageId) return;
    if (isAtBottomRef.current) {
      scrollToBottom('smooth');
    }
  }, [messages, streamingMessage, streamingBlocks, currentTodos, targetMessageId, scrollToBottom]);

  // Scroll to and highlight a search-linked message once the conversation has loaded (it
  // loads asynchronously for a cold-opened session). The `m` param is consumed as soon as
  // messages are present — whether or not the target is found — so a deleted/absent target
  // can't leave a stale `?m=` in the URL and permanently suppress the bottom auto-scroll.
  useEffect(() => {
    if (!targetMessageId) return;
    if (messages.length === 0) return; // not loaded yet — keep waiting
    const el = document.getElementById(`message-${targetMessageId}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      isAtBottomRef.current = false;
      setHighlightId(targetMessageId);
    }
    const next = new URLSearchParams(searchParams);
    next.delete('m');
    setSearchParams(next, { replace: true });
  }, [targetMessageId, messages, searchParams, setSearchParams]);

  // Clear the highlight after it has had time to show; kept separate so consuming the
  // `m` param (which flips targetMessageId to null) doesn't cancel the timer.
  useEffect(() => {
    if (!highlightId) return;
    const t = setTimeout(() => setHighlightId(null), 1600);
    return () => clearTimeout(t);
  }, [highlightId]);

  // Re-pin the list to the bottom on any late layout shift, but only when the user was
  // already there. Two sources, both off the `messages` dependency so the bottom
  // auto-scroll above never fires for them:
  //   - the viewport shrinks: the composer auto-grows with its content, or the connector
  //     header gains a row once its status resolves ~1s after a cold session open (the
  //     drift users saw on load). Both are flex siblings of the viewport, so its own box
  //     resizing catches them.
  //   - the content grows in flow: a connector notice appears, code blocks finish
  //     highlighting, images load — scrollHeight grows without resizing the viewport.
  // Instant (not smooth) so it tracks tightly and doesn't fight a still-settling layout.
  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (targetMessageId) return; // don't fight a pending deep-link jump
      if (isAtBottomRef.current) scrollToBottom('auto');
    });
    if (containerRef.current) ro.observe(containerRef.current);
    if (contentRef.current) ro.observe(contentRef.current);
    return () => ro.disconnect();
  }, [targetMessageId, scrollToBottom]);

  // Always scroll to bottom when streaming starts
  useEffect(() => {
    if (isStreaming) {
      isAtBottomRef.current = true;
      scrollToBottom('smooth');
    }
  }, [isStreaming, scrollToBottom]);

  const tabClass = (active: boolean) =>
    cn(
      'px-4 py-2 border-b-2 transition-colors',
      active ? 'border-blue-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'
    );

  return (
    <div className="flex-1 flex flex-col h-full relative min-h-0 overflow-hidden">
      <ErrorBanner />

      {/* Always render the bar so its height is stable from first paint — the egress
          socket connects a beat after load (ws-ticket fetch + handshake), so gating the
          bar on it made the tabs pop in and shoved the conversation down. The Network
          tab stays disabled until egress is actually available. */}
      <div className="flex shrink-0 border-b border-gray-800 bg-gray-900/40 text-sm">
        <button onClick={() => setTab('chat')} className={tabClass(tab === 'chat')}>
          Conversation
        </button>
        <button
          onClick={() => egressAvailable && setTab('egress')}
          disabled={!egressAvailable}
          title={egressAvailable ? undefined : 'Network activity appears once the session sandbox is active'}
          className={cn(
            tabClass(tab === 'egress'),
            'flex items-center gap-1.5',
            !egressAvailable && 'opacity-50 cursor-not-allowed hover:text-gray-400',
          )}
        >
          Network
          {egressAvailable && (
            <>
              <span className="text-[10px] px-1 rounded bg-gray-700 text-gray-300">
                {egress.events.length}
              </span>
              {blockedCount > 0 && (
                <span className="text-[10px] px-1 rounded bg-red-900/50 text-red-400">
                  {blockedCount} blocked
                </span>
              )}
            </>
          )}
        </button>
      </div>

      {/* Chat stays mounted (just hidden) when viewing egress, to preserve scroll + state. */}
      <div className={cn('flex-1 flex flex-col min-h-0', showEgress && 'hidden')}>
        {sessionId && <ConnectorPanel sessionId={sessionId} />}
        <div
          ref={containerRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0"
        >
          <div ref={contentRef}>
            {sessionId && <ConnectorInlineNotice sessionId={sessionId} />}
            <MessageList messages={messages} highlightedId={highlightId} />
            {isStreaming && <div className={compact ? 'mt-4' : 'mt-6'}><StreamingMessage /></div>}
          </div>
        </div>
        <ChatInput onSend={onSendMessage} onStop={onStopStreaming} disabled={!isConnected} isStreaming={isStreaming} initialPrompt={initialPrompt} autoSubmit={autoSubmit} sessionId={sessionId} />
      </div>

      {showEgress && <EgressLog events={egress.events} connected={egress.connected} />}

      <FloatingTodoPanel />
    </div>
  );
}
