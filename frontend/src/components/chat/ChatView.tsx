import { useEffect, useRef, useCallback, useState } from 'react';
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);

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

  // Auto-scroll only if user was at bottom
  useEffect(() => {
    if (isAtBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, streamingMessage, streamingBlocks, currentTodos]);

  // Always scroll to bottom when streaming starts
  useEffect(() => {
    if (isStreaming) {
      isAtBottomRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [isStreaming]);

  const tabClass = (active: boolean) =>
    cn(
      'px-4 py-2 border-b-2 transition-colors',
      active ? 'border-blue-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'
    );

  return (
    <div className="flex-1 flex flex-col h-full relative min-h-0 overflow-hidden">
      <ErrorBanner />

      {egressAvailable && (
        <div className="flex shrink-0 border-b border-gray-800 bg-gray-900/40 text-sm">
          <button onClick={() => setTab('chat')} className={tabClass(tab === 'chat')}>
            Conversation
          </button>
          <button
            onClick={() => setTab('egress')}
            className={cn(tabClass(tab === 'egress'), 'flex items-center gap-1.5')}
          >
            Network
            <span className="text-[10px] px-1 rounded bg-gray-700 text-gray-300">
              {egress.events.length}
            </span>
            {blockedCount > 0 && (
              <span className="text-[10px] px-1 rounded bg-red-900/50 text-red-400">
                {blockedCount} blocked
              </span>
            )}
          </button>
        </div>
      )}

      {/* Chat stays mounted (just hidden) when viewing egress, to preserve scroll + state. */}
      <div className={cn('flex-1 flex flex-col min-h-0', showEgress && 'hidden')}>
        {sessionId && <ConnectorPanel sessionId={sessionId} />}
        <div
          ref={containerRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0"
        >
          <MessageList messages={messages} />
          {isStreaming && <div className={compact ? 'mt-4' : 'mt-6'}><StreamingMessage /></div>}
          <div ref={bottomRef} />
        </div>
        <ChatInput onSend={onSendMessage} onStop={onStopStreaming} disabled={!isConnected} isStreaming={isStreaming} initialPrompt={initialPrompt} autoSubmit={autoSubmit} sessionId={sessionId} />
      </div>

      {showEgress && <EgressLog events={egress.events} connected={egress.connected} />}

      <FloatingTodoPanel />
    </div>
  );
}
