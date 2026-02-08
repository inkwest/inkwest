import { useEffect, useRef, useCallback } from 'react';
import { useChatStore } from '@/store/chat';
import { useSettingsStore } from '@/store/settings';
import { MessageList } from './MessageList';
import { ChatInput } from './ChatInput';
import { StreamingMessage } from './StreamingMessage';
import { ErrorBanner } from './ErrorBanner';
import { FloatingTodoPanel } from './FloatingTodoPanel';

interface Props {
  onSendMessage: (content: string) => void;
  onStopStreaming?: () => void;
  initialPrompt?: string | null;
}

export function ChatView({ onSendMessage, onStopStreaming, initialPrompt }: Props) {
  const { messages, streamingMessage, isStreaming, isConnected, streamingBlocks, currentTodos } = useChatStore();
  const compact = useSettingsStore((s) => s.compactToolCards);
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);

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

  return (
    <div className="flex-1 flex flex-col h-full relative min-h-0 overflow-hidden">
      <ErrorBanner />
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto overflow-x-hidden p-4 min-h-0"
      >
        <MessageList messages={messages} />
        {isStreaming && <div className={compact ? 'mt-4' : 'mt-6'}><StreamingMessage /></div>}
        <div ref={bottomRef} />
      </div>
      <FloatingTodoPanel />
      <ChatInput onSend={onSendMessage} onStop={onStopStreaming} disabled={!isConnected} isStreaming={isStreaming} initialPrompt={initialPrompt} />
    </div>
  );
}
