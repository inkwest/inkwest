import { useState, useEffect, useRef, KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { useDraftsStore } from '@/store/drafts';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { PromptTemplatePalette } from './PromptTemplatePalette';

interface Props {
  onSend: (message: string) => void;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  initialPrompt?: string | null;
  // Submit initialPrompt automatically once the composer is enabled (WS connected).
  // The text stays in the textarea until then, so it's recoverable if connect fails.
  autoSubmit?: boolean;
  sessionId?: string;
  // Centered "start a conversation" variant: taller textarea, no top divider.
  large?: boolean;
  placeholder?: string;
}

export function ChatInput({ onSend, onStop, disabled, isStreaming, initialPrompt, autoSubmit, sessionId, large, placeholder }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);
  const setDraft = useDraftsStore((s) => s.setDraft);
  const clearDraft = useDraftsStore((s) => s.clearDraft);
  // Seed once from the persisted per-session draft so switching sessions (which
  // unmounts this component behind the "Loading session..." guard) and coming
  // back restores what the user had typed.
  const [message, setMessage] = useState(() =>
    sessionId ? useDraftsStore.getState().drafts[sessionId] ?? '' : ''
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const initialPromptApplied = useRef(false);
  const [shimmer, setShimmer] = useState(false);

  // Mirror edits into the persisted draft store keyed by session.
  const updateMessage = (value: string) => {
    setMessage(value);
    if (sessionId) setDraft(sessionId, value);
  };

  useEffect(() => {
    if (!initialPrompt || initialPromptApplied.current) return;
    initialPromptApplied.current = true;
    // A restored draft takes precedence over a one-shot initial prompt.
    if (sessionId && useDraftsStore.getState().drafts[sessionId]) return;
    setMessage(initialPrompt);
    if (sessionId) setDraft(sessionId, initialPrompt);
    setShimmer(true);
    setTimeout(() => textareaRef.current?.focus(), 100);
    const timer = setTimeout(() => setShimmer(false), 4000);
    return () => clearTimeout(timer);
  }, [initialPrompt, sessionId, setDraft]);

  const handleSend = () => {
    setShimmer(false);
    // Don't send while a response is streaming — the button is a Stop button then,
    // but Enter would otherwise still fire and the backend rejects a second turn
    // ("A response is already in progress").
    if (message.trim() && !disabled && !isStreaming) {
      onSend(message.trim());
      setMessage('');
      if (sessionId) clearDraft(sessionId);
    }
  };

  // Auto-submit the handed-off prompt once the WS is connected. Only fires for the
  // untouched seed: if the user has started editing it, we defer to them. If connect
  // never happens, the text simply stays in the composer (recoverable).
  const autoSubmittedRef = useRef(false);
  useEffect(() => {
    if (!autoSubmit || autoSubmittedRef.current) return;
    if (disabled || isStreaming) return;
    if (!initialPrompt || message.trim() !== initialPrompt.trim()) return;
    autoSubmittedRef.current = true;
    handleSend();
    // handleSend reads current message/disabled; this is a guarded one-shot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSubmit, disabled, isStreaming, message, initialPrompt]);

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSelectTemplate = (content: string) => {
    // Replace input with the template, leaving it editable before send.
    updateMessage(content);
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  // Square corner button sizing for the large "start a conversation" composer.
  const square = large ? 'h-12 w-12 px-0 shrink-0' : 'px-3';
  const iconSize = large ? 'h-5 w-5' : 'h-4 w-4';

  return (
    <div className={large ? '' : 'border-t border-gray-700 p-4'}>
      {/* Large variant anchors the skill library to the NW corner and submit to the
          SE corner via top-/bottom-aligned side columns, with a tall textarea between. */}
      <div className="flex items-stretch gap-2">
        <div className={large ? 'flex flex-col justify-start' : 'contents'}>
          <PromptTemplatePalette
            onSelect={handleSelectTemplate}
            disabled={disabled}
            className={large ? square : undefined}
          />
        </div>
        <textarea
          ref={textareaRef}
          value={message}
          onChange={(e) => updateMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          autoFocus
          placeholder={placeholder ?? 'Describe what you want to investigate or analyse…'}
          disabled={disabled}
          className={cn(
            'flex-1 bg-gray-700 border-2 border-gray-600 rounded-lg px-4 py-2 resize-none focus:outline-none focus:border-blue-500 disabled:opacity-50',
            shimmer && message.trim() && 'shimmer-border',
            large ? 'text-base' : TEXT_SIZE.body(compact),
          )}
          rows={large ? 6 : 1}
        />
        <div className={large ? 'flex flex-col justify-end' : 'contents'}>
          {isStreaming ? (
            <button
              onClick={onStop}
              aria-label="Stop"
              className={cn(
                'flex items-center justify-center rounded-lg border border-transparent bg-red-600 hover:bg-red-700',
                square,
              )}
            >
              <Square className={iconSize} />
            </button>
          ) : (
            <button
              onClick={handleSend}
              aria-label="Send message"
              disabled={disabled || !message.trim()}
              className={cn(
                'flex items-center justify-center rounded-lg border border-transparent bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed',
                square,
                shimmer && message.trim() && 'animate-flash-btn',
              )}
            >
              <Send className={iconSize} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
