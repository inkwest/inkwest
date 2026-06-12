import { useState, useEffect, useMemo, useRef, KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { useDraftsStore } from '@/store/drafts';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { Skill } from '@/types/api';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { SkillPalette } from './SkillPalette';
import { SkillAutocomplete } from './SkillAutocomplete';
import { formatArgs } from '@/lib/skillArgs';

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

// The "/<query>" token immediately left of the cursor (slash at message start or after
// whitespace), or null. Lets the skill autocomplete trigger ANYWHERE in the message.
function slashTokenBeforeCursor(text: string, cursor: number): string | null {
  const m = text.slice(0, cursor).match(/(?:^|\s)\/(\S*)$/);
  return m ? m[1] : null;
}

// Replace the whole "/word" token around the cursor with `/<slug> `.
function replaceSlashToken(
  text: string,
  cursor: number,
  slug: string,
): { text: string; cursor: number } {
  const before = text.slice(0, cursor);
  const startMatch = before.match(/(?:^|\s)(\/\S*)$/);
  const slashAt = startMatch ? before.length - startMatch[1].length : cursor;
  let end = cursor;
  while (end < text.length && !/\s/.test(text[end])) end++;
  const insert = `/${slug} `;
  return {
    text: text.slice(0, slashAt) + insert + text.slice(end),
    cursor: slashAt + insert.length,
  };
}

// Insert text at the cursor (used by the palette, which has no token to replace).
function insertAtCursor(
  text: string,
  cursor: number,
  insert: string,
): { text: string; cursor: number } {
  return {
    text: text.slice(0, cursor) + insert + text.slice(cursor),
    cursor: cursor + insert.length,
  };
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
  const [focused, setFocused] = useState(false);
  // "Invite to prompt": rotating-gradient border on a pristine new conversation.
  // The start screen (large) is new by definition — the chat store may still hold
  // the previous session's messages there, since resetChat only runs in ChatPage.
  const noMessages = useChatStore((s) => s.messages.length === 0);
  const inviteToPrompt = (large || noMessages) && !message.trim() && !focused;

  // Inline "/" skill autocomplete (skills surfaced as Claude skills).
  const templates = useSkillsStore((s) => s.templates);
  const fetchTemplates = useSkillsStore((s) => s.fetchTemplates);
  const [skillIndex, setSkillIndex] = useState(0);
  const [skillDismissed, setSkillDismissed] = useState(false);
  const [cursor, setCursor] = useState(0);
  // Active while the token left of the cursor is a "/<query>" — so a skill can be
  // triggered anywhere in the message, not only at the start.
  const skillQuery = useMemo(
    () => slashTokenBeforeCursor(message, cursor)?.toLowerCase() ?? null,
    [message, cursor],
  );
  const skillMatches = useMemo(() => {
    if (skillQuery === null) return [];
    return templates
      .filter(
        (t) =>
          t.slug.includes(skillQuery) ||
          t.name.toLowerCase().includes(skillQuery) ||
          (t.description || '').toLowerCase().includes(skillQuery),
      )
      .slice(0, 8);
  }, [skillQuery, templates]);
  const showSkills = skillQuery !== null && skillMatches.length > 0 && !skillDismissed;
  // The skill currently driving the input (message begins with a known "/<slug>"). Used
  // to show an argument hint under the composer once a skill is picked and the native
  // placeholder is gone (the field is no longer empty).
  const activeSkill = useMemo(() => {
    const m = message.match(/^\/(\S+)(?:\s|$)/);
    return m ? templates.find((t) => t.slug === m[1]) ?? null : null;
  }, [message, templates]);

  // Mirror edits into the persisted draft store keyed by session.
  const updateMessage = (value: string) => {
    setMessage(value);
    if (sessionId) setDraft(sessionId, value);
    setSkillDismissed(false); // re-typing reopens a dropdown closed via Escape
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

  // Load templates once so the "/" autocomplete has data without opening the palette.
  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  // Keep the highlighted item valid as the query / result set changes.
  useEffect(() => {
    setSkillIndex(0);
  }, [skillQuery]);

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
    if (showSkills) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSkillIndex((i) => (i + 1) % skillMatches.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSkillIndex((i) => (i - 1 + skillMatches.length) % skillMatches.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        selectSkill(skillMatches[skillIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSkillDismissed(true);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Apply a text edit, then restore focus + caret after React commits the new value.
  const applyEdit = (next: { text: string; cursor: number }) => {
    updateMessage(next.text);
    setCursor(next.cursor); // sync: the dropdown/hint must react immediately (no flicker)
    setTimeout(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(next.cursor, next.cursor);
    }, 0);
  };

  // Autocomplete pick: replace the "/<query>" token around the cursor with "/<slug> ".
  const selectSkill = (template: Skill) =>
    applyEdit(replaceSlashToken(message, cursor, template.slug));

  // Palette pick: insert "/<slug> " at the cursor (no token to replace).
  const insertSkillFromPalette = (slug: string) =>
    applyEdit(insertAtCursor(message, cursor, `/${slug} `));

  // Square corner button sizing for the large "start a conversation" composer.
  const square = large ? 'h-12 w-12 px-0 shrink-0' : 'px-3';
  const iconSize = large ? 'h-5 w-5' : 'h-4 w-4';

  return (
    <div className={large ? '' : 'border-t border-gray-700 p-4'}>
      {/* Large variant anchors the skill library to the NW corner and submit to the
          SE corner via top-/bottom-aligned side columns, with a tall textarea between. */}
      <div className="flex items-stretch gap-2">
        <div className={large ? 'flex flex-col justify-start' : 'contents'}>
          <SkillPalette
            onSelect={insertSkillFromPalette}
            disabled={disabled}
            className={large ? square : undefined}
          />
        </div>
        <div className="relative flex-1">
          {showSkills && (
            <SkillAutocomplete
              matches={skillMatches}
              activeIndex={skillIndex}
              onSelect={selectSkill}
              onHover={setSkillIndex}
            />
          )}
          <textarea
            ref={textareaRef}
            value={message}
            onChange={(e) => {
              updateMessage(e.target.value);
              setCursor(e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyDown={handleKeyDown}
            onSelect={(e) => setCursor(e.currentTarget.selectionStart ?? 0)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            autoFocus
            placeholder={placeholder ?? 'Describe what you want to investigate or analyse…'}
            disabled={disabled}
            className={cn(
              'w-full bg-gray-700 border-2 border-gray-600 rounded-lg px-4 py-2 resize-none focus:outline-none focus:border-blue-500 focus:shadow-[0_0_20px_3px_rgba(59,130,246,0.45)] disabled:opacity-50',
              shimmer && message.trim() && 'shimmer-border',
              inviteToPrompt && 'invite-border',
              large ? 'text-base' : TEXT_SIZE.body(compact),
            )}
            rows={large ? 6 : 1}
          />
        </div>
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
      {activeSkill && !showSkills && (
        <div className="mt-1 px-1 text-xs text-gray-500 truncate">
          <span className="text-gray-400">/{activeSkill.slug}</span>
          {` ${activeSkill.argument_hint ? formatArgs(activeSkill.argument_hint) : 'arguments libres'}`}
          {activeSkill.description ? ` · ${activeSkill.description}` : ''}
        </div>
      )}
    </div>
  );
}
