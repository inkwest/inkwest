import { useState, useEffect, useRef, KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { PromptTemplatePalette } from './PromptTemplatePalette';

interface Props {
  onSend: (message: string) => void;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  initialPrompt?: string | null;
}

export function ChatInput({ onSend, onStop, disabled, isStreaming, initialPrompt }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);
  const [message, setMessage] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const initialPromptApplied = useRef(false);
  const [shimmer, setShimmer] = useState(false);

  useEffect(() => {
    if (!initialPrompt || initialPromptApplied.current) return;
    initialPromptApplied.current = true;
    setMessage(initialPrompt);
    setShimmer(true);
    setTimeout(() => textareaRef.current?.focus(), 100);
    const timer = setTimeout(() => setShimmer(false), 4000);
    return () => clearTimeout(timer);
  }, [initialPrompt]);

  const handleSend = () => {
    setShimmer(false);
    if (message.trim() && !disabled) {
      onSend(message.trim());
      setMessage('');
    }
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSelectTemplate = (content: string) => {
    // Replace input with the template, leaving it editable before send.
    setMessage(content);
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  return (
    <div className="border-t border-gray-700 p-4">
      <div className="flex items-stretch gap-2">
        <PromptTemplatePalette onSelect={handleSelectTemplate} disabled={disabled} />
        <textarea
          ref={textareaRef}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          autoFocus
          placeholder="Describe your Kubernetes issue..."
          disabled={disabled}
          className={cn(
            'flex-1 bg-gray-700 border-2 border-gray-600 rounded-lg px-4 py-2 resize-none focus:outline-none focus:border-blue-500 disabled:opacity-50',
            shimmer && message.trim() && 'shimmer-border',
            TEXT_SIZE.body(compact),
          )}
          rows={1}
        />
        {isStreaming ? (
          <button
            onClick={onStop}
            aria-label="Stop"
            className="flex items-center justify-center px-3 rounded-lg border border-transparent bg-red-600 hover:bg-red-700"
          >
            <Square className="h-4 w-4" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            aria-label="Send message"
            disabled={disabled || !message.trim()}
            className={cn(
              'flex items-center justify-center px-3 rounded-lg border border-transparent bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed',
              shimmer && message.trim() && 'animate-flash-btn',
            )}
          >
            <Send className="h-4 w-4" />
          </button>
        )}
      </div>
    </div>
  );
}
