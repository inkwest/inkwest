import { useEffect, useLayoutEffect, useRef, useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Share2 } from 'lucide-react';
import { useChatStore } from '@/store/chat';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { useEgressSocket, isBlocked, isError } from '@/hooks/useEgressSocket';
import { useSessionMetrics } from '@/hooks/useSessionMetrics';
import { MessageList } from './MessageList';
import { ChatInput } from './ChatInput';
import { SessionModelSelect } from './SessionModelSelect';
import { PresenceBar } from './PresenceBar';
import { ShareModal } from './ShareModal';
import { StreamingMessage } from './StreamingMessage';
import { ErrorBanner } from './ErrorBanner';
import { ReconnectingBanner } from './ReconnectingBanner';
import { EgressLog } from './EgressPanel';
import { MonitoringPanel } from './MonitoringPanel';
import { ConnectorPanel } from '@/components/connector/ConnectorPanel';
import { ConnectorInlineNotice } from './ConnectorInlineNotice';
import { McpInlineNotice } from './McpInlineNotice';
import { Attachment } from '@/types/api';
import { cn } from '@/lib/utils';

interface Props {
  onSendMessage: (content: string, attachments?: Attachment[]) => void;
  onStopStreaming?: () => void;
  onRetryMessage?: (messageId: string) => void;
  initialPrompt?: string | null;
  // Attachments handed off from the start screen, seeded into the composer alongside
  // initialPrompt so the auto-submit carries them.
  initialAttachments?: Attachment[] | null;
  // Auto-submit initialPrompt once the composer is enabled (WS connected).
  autoSubmit?: boolean;
  sessionId?: string;
  // Read-only viewer (a shared-in session or the public link view): hide the composer.
  isReadOnly?: boolean;
  // Notify peers of typing state (owner only); no-op when omitted.
  onTyping?: (state: boolean) => void;
}

export function ChatView({ onSendMessage, onStopStreaming, onRetryMessage, initialPrompt, initialAttachments, autoSubmit, sessionId, isReadOnly, onTyping }: Props) {
  const { messages, streamingMessage, isStreaming, isConnected, isReconnecting, streamingBlocks } = useChatStore();
  const compact = useSettingsStore((s) => s.compactToolCards);
  const wideComposer = useSettingsStore((s) => s.wideComposer);
  // Share lives on the conversation toolbar (this tab row). Owner only — a session
  // shared *with* the user (is_owner === false) shows no Share control. Primitive
  // selectors so ChatView doesn't re-render on unrelated currentSession changes
  // (e.g. token counts updating every turn).
  const [showShare, setShowShare] = useState(false);
  const canShare = useSessionsStore(
    (s) => !!sessionId && s.currentSession?.id === sessionId && s.currentSession?.is_owner !== false
  );
  const shareCount = useSessionsStore((s) =>
    s.currentSession?.id === sessionId ? (s.currentSession?.share_count ?? 0) : 0
  );
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);
  // The floating composer overlaps the scroll area; pad the messages by its measured
  // height so the last one always clears it, even as the composer grows with text.
  const composerRef = useRef<HTMLDivElement>(null);
  const [composerH, setComposerH] = useState(0);
  // useLayoutEffect: set the height before paint so the bottom padding is correct on
  // the very first render — the on-load scroll-to-bottom then lands past the composer
  // instead of leaving the last message hidden under it.
  useLayoutEffect(() => {
    const el = composerRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setComposerH(el.offsetHeight));
    ro.observe(el);
    setComposerH(el.offsetHeight);
    return () => ro.disconnect();
  }, []);

  // Deep-link from a search hit: `?m=<messageId>` scrolls that message into view and
  // highlights it briefly. The param is consumed (cleared) once handled so new messages
  // or re-renders don't re-trigger the jump.
  const [searchParams, setSearchParams] = useSearchParams();
  const targetMessageId = searchParams.get('m');
  const [highlightId, setHighlightId] = useState<string | null>(null);

  // Live network-egress audit (sandbox only). The WS stays subscribed regardless of the
  // active tab so the counter keeps updating while the user is on the Conversation tab.
  const egress = useEgressSocket(sessionId);
  const metrics = useSessionMetrics(sessionId);
  const [tab, setTab] = useState<'chat' | 'egress' | 'monitoring'>('chat');
  const egressAvailable = egress.connected || egress.events.length > 0;
  const blockedCount = egress.events.filter(isBlocked).length;
  const errorCount = egress.events.filter(isError).length;
  const showEgress = egressAvailable && tab === 'egress';
  const metricsAvailable = metrics.status === 'running' || metrics.samples.length > 0;
  const showMonitoring = metricsAvailable && tab === 'monitoring';

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
  }, [messages, streamingMessage, streamingBlocks, targetMessageId, scrollToBottom]);

  // The composer's measured height feeds the bottom padding; when it settles (on load:
  // 0 → real) or grows, re-pin to the bottom so the last message stays clear of it.
  useEffect(() => {
    if (!targetMessageId && isAtBottomRef.current) scrollToBottom('auto');
  }, [composerH, targetMessageId, scrollToBottom]);

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
      {isReconnecting && <ReconnectingBanner />}

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
              {errorCount > 0 && (
                <span className="text-[10px] px-1 rounded bg-amber-900/50 text-amber-400">
                  {errorCount} errors
                </span>
              )}
            </>
          )}
        </button>
        <button
          onClick={() => metricsAvailable && setTab('monitoring')}
          disabled={!metricsAvailable}
          title={metricsAvailable ? undefined : 'Resource graphs appear once the session sandbox is active'}
          className={cn(
            tabClass(tab === 'monitoring'),
            'flex items-center gap-1.5',
            !metricsAvailable && 'opacity-50 cursor-not-allowed hover:text-gray-400',
          )}
        >
          Monitoring
          {metrics.status === 'running' && (
            <span className="h-1.5 w-1.5 rounded-full bg-green-400 animate-pulse" />
          )}
        </button>

        {/* Conversation action (not a tab): right-aligned, reflects shared state. */}
        {canShare && (
          <button
            onClick={() => setShowShare(true)}
            title={shareCount > 0 ? `Shared with ${shareCount} · manage` : 'Share conversation'}
            className={cn(
              'ml-auto flex items-center gap-1.5 px-4 py-2 transition-colors',
              shareCount > 0 ? 'text-blue-400 hover:text-blue-300' : 'text-gray-400 hover:text-gray-200'
            )}
          >
            <Share2 className="h-4 w-4" />
            <span>{shareCount > 0 ? `Shared·${shareCount}` : 'Share'}</span>
          </button>
        )}
      </div>

      {/* Chat stays mounted (just hidden) on the other tabs, to preserve scroll + state.
          The messages area fills the full height (its scrollbar spans the whole
          conversation) and the composer floats over the bottom as an overlay — the
          last messages clear it via the scroll area's bottom padding. */}
      <div className={cn('relative flex-1 flex flex-col min-h-0', (showEgress || showMonitoring) && 'hidden')}>
        <PresenceBar />
        <div
          ref={containerRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0"
          style={{ paddingBottom: composerH + 32 }}
        >
          <div ref={contentRef}>
            {sessionId && <ConnectorInlineNotice sessionId={sessionId} />}
            <McpInlineNotice sessionId={sessionId} />
            <MessageList messages={messages} highlightedId={highlightId} onRetry={onRetryMessage} />
            {isStreaming && <div className={compact ? 'mt-4' : 'mt-6'}><StreamingMessage /></div>}
          </div>
        </div>
        {isReadOnly ? (
          <div className="shrink-0 border-t border-gray-800 px-4 py-3 text-center text-xs text-gray-500">
            You are viewing this conversation — read-only.
          </div>
        ) : (
          // Floating composer overlay. `right-4` keeps it off the messages scrollbar;
          // the card is frosted glass and the pointer-events-none gutter lets scroll
          // pass through to the messages behind it.
          <div className="pointer-events-none absolute bottom-0 left-0 right-4">
            <div ref={composerRef} className="pointer-events-auto">
              <div className={cn('mx-auto', wideComposer ? 'max-w-none' : 'max-w-[72rem]')}>
                <ChatInput
                  onSend={onSendMessage}
                  onStop={onStopStreaming}
                  disabled={!isConnected}
                  isStreaming={isStreaming}
                  initialPrompt={initialPrompt}
                  autoSubmit={autoSubmit}
                  initialAttachments={initialAttachments}
                  sessionId={sessionId}
                  modelControl={<SessionModelSelect />}
                  connectorControls={sessionId ? <ConnectorPanel sessionId={sessionId} layout="icons" /> : undefined}
                  onTyping={onTyping}
                />
              </div>
            </div>
          </div>
        )}
      </div>

      {showEgress && <EgressLog events={egress.events} connected={egress.connected} />}
      {showMonitoring && <MonitoringPanel samples={metrics.samples} status={metrics.status} />}

      {canShare && sessionId && (
        <ShareModal sessionId={sessionId} isOpen={showShare} onClose={() => setShowShare(false)} />
      )}
    </div>
  );
}
