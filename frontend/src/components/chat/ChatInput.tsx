import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { useDraftsStore } from '@/store/drafts';
import { useComposerStore } from '@/store/composer';
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
  const composerNonce = useComposerStore((s) => s.nonce);
  const consumeComposer = useComposerStore((s) => s.consume);
  // Seed once from the persisted per-session draft so switching sessions (which
  // unmounts this component behind the "Loading session..." guard) and coming
  // back restores what the user had typed.
  const [message, setMessage] = useState(() =>
    sessionId ? useDraftsStore.getState().drafts[sessionId] ?? '' : ''
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Mirrors the latest `message` for effects that run on a non-`message` dep
  // (the inject effect) and must read the current value without a stale closure.
  const messageRef = useRef(message);
  messageRef.current = message;
  const initialPromptApplied = useRef(false);
  const [shimmer, setShimmer] = useState(false);
  const [focused, setFocused] = useState(false);
  // "Invite to prompt": rotating-gradient border on a pristine new conversation.
  // The start screen (large) is new by definition — the chat store may still hold
  // the previous session's messages there, since resetChat only runs in ChatPage.
  const noMessages = useChatStore((s) => s.messages.length === 0);
  const inviteToPrompt = (large || noMessages) && !message.trim() && !focused;

  // Shell-style ArrowUp/Down prompt recall. null = editing live; otherwise an index into
  // the recalled-prompts list (see collectPrompts). historyStash holds the live draft
  // saved on entry so stepping past the newest prompt restores it. The prompt list is
  // read on demand from the store (collectPrompts) rather than via a subscription: it's
  // only needed at keypress time, so this avoids extra re-renders and any index drift if
  // a real-time sync reorders messages mid-navigation.
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const historyStash = useRef('');

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

  // Auto-grow the composer with its content, capped at ~15 lines (then it scrolls —
  // see max-h on the textarea). Applies to both variants: the large start-screen box
  // keeps its tall 6-row floor (min-h) but grows from there.
  //
  // `allowShrink` does the 'auto' remeasure that lets the box get *shorter*. We gate it
  // because resetting to 'auto' collapses the textarea to its 1-row floor, and since the
  // textarea is a flex sibling of the message list, that momentary collapse grows the
  // list's viewport — the browser then clamps the viewport's scrollTop to the smaller max
  // and never restores it, a visible upward scroll jump. On the common forward-typing
  // path scrollHeight already reflects the (overflowing) content, so we size to it without
  // collapsing. A real net shrink still fires ChatView's ResizeObserver, which re-pins.
  const resize = useCallback((allowShrink: boolean) => {
    const ta = textareaRef.current;
    if (!ta) return;
    if (allowShrink) ta.style.height = 'auto';
    // The textarea is border-box with a 2px border; scrollHeight excludes the
    // border, so add it back or the box ends up a few px short (text clips, and
    // a single line no longer matches the send button's height).
    const borderY = ta.offsetHeight - ta.clientHeight;
    ta.style.height = `${ta.scrollHeight + borderY}px`;
  }, []);

  // Re-size on content change. Only allow shrink when the text actually got shorter
  // (deletion); typing forward keeps the box from collapsing. useLayoutEffect resizes
  // before paint so there's no one-frame flash.
  const prevLenRef = useRef(0);
  useLayoutEffect(() => {
    resize(message.length < prevLenRef.current);
    prevLenRef.current = message.length;
  }, [message, large, resize]);

  // Re-size on width change (sidebar drag, window resize): the content-change effect
  // doesn't fire then, so the box would keep a stale pixel height and re-wrapped text
  // would clip/scroll. Width changes are rare, so always allow shrink here. Guarding on
  // the observed width ignores the resize our own height writes trigger (no loop).
  const prevWidthRef = useRef(0);
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0].contentRect.width;
      if (width === prevWidthRef.current) return; // our own height write, not a width change
      prevWidthRef.current = width;
      resize(true);
    });
    ro.observe(ta);
    return () => ro.disconnect();
  }, [resize]);

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
      setHistoryIndex(null);
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

  // Escape stops an in-flight response from anywhere on the page, not only when the
  // composer is focused (the user is usually watching the stream, not the input).
  // Skipped while the "/" skill dropdown is open — there Escape dismisses the dropdown
  // (handled in handleKeyDown) rather than stopping.
  useEffect(() => {
    if (!isStreaming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || showSkills) return;
      e.preventDefault();
      onStop?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isStreaming, showSkills, onStop]);

  const handleKeyDown = (e: ReactKeyboardEvent) => {
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
    // Shell-style prompt recall. ArrowUp walks to an older prompt only when the caret is
    // on the first line (no selection), ArrowDown to a newer one only on the last line —
    // so within a multi-line draft or recalled prompt the arrows still move the caret
    // normally, and recall only kicks in at the edges. navigateHistory returns false when
    // there's nothing to recall, leaving the default caret movement intact.
    // Accepted edges: a recalled bare "/slug" re-opens the skill dropdown (handled above,
    // so arrows drive it until Escape); and under fast key-repeat a second press may read
    // the pre-recall caret before applyEdit's setTimeout restores it — both benign.
    const ta = e.currentTarget as HTMLTextAreaElement;
    const collapsed = ta.selectionStart === ta.selectionEnd;
    if (e.key === 'ArrowUp' && !e.shiftKey && collapsed) {
      // No newline before the caret. Use slice rather than lastIndexOf(\n, start-1):
      // a negative fromIndex clamps to 0 instead of "search nothing", so caret-at-0 on
      // text that begins with \n would wrongly report not-first-line.
      const onFirstLine = ta.value.slice(0, ta.selectionStart).indexOf('\n') === -1;
      if (onFirstLine && navigateHistory('up')) {
        e.preventDefault();
        return;
      }
    }
    if (e.key === 'ArrowDown' && !e.shiftKey && collapsed) {
      const onLastLine = ta.value.indexOf('\n', ta.selectionEnd) === -1;
      if (onLastLine && navigateHistory('down')) {
        e.preventDefault();
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Apply a text edit, then restore focus + caret after React commits the new value.
  // `persist` mirrors the text into the per-session draft store (the default for skill
  // picks and injects). History recall passes persist=false: the textarea shows the
  // recalled prompt, but the persisted draft keeps the user's last manually-typed text,
  // so abandoning a recall (e.g. switching sessions) restores their own draft, not an
  // old prompt. A manual edit (onChange) or send then persists/clears as usual.
  const applyEdit = (next: { text: string; cursor: number }, persist = true) => {
    if (persist) {
      updateMessage(next.text);
    } else {
      setMessage(next.text);
      setSkillDismissed(false); // keep parity with updateMessage's dropdown reset
    }
    setCursor(next.cursor); // sync: the dropdown/hint must react immediately (no flicker)
    setTimeout(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(next.cursor, next.cursor);
    }, 0);
  };

  // Recallable prompts (oldest→newest): plain-string user messages only (skip tool
  // results / block content), with consecutive duplicates collapsed so a repeated send
  // doesn't need multiple ArrowUp presses to step past. Read fresh from the store so the
  // list always reflects the current conversation.
  const collectPrompts = (): string[] =>
    useChatStore.getState().messages.reduce<string[]>((acc, m) => {
      if (
        m.role === 'user' &&
        typeof m.content === 'string' &&
        m.content.trim() &&
        m.content !== acc[acc.length - 1]
      ) {
        acc.push(m.content);
      }
      return acc;
    }, []);

  // Shell-style prompt recall. ArrowUp walks toward older prompts, ArrowDown back toward
  // newer; stepping past the newest restores the stashed live draft and exits history.
  // The boundary gating in handleKeyDown (first line for up, last line for down) lets the
  // caret still move within a multi-line recalled prompt. Returns false (don't
  // preventDefault, let the caret move normally) when there's nothing to recall.
  const navigateHistory = (dir: 'up' | 'down'): boolean => {
    const prompts = collectPrompts();
    if (prompts.length === 0) return false;
    if (dir === 'up') {
      const idx = historyIndex === null ? prompts.length - 1 : Math.max(0, historyIndex - 1);
      if (historyIndex === null) historyStash.current = messageRef.current;
      setHistoryIndex(idx);
      applyEdit({ text: prompts[idx], cursor: prompts[idx].length }, false);
      return true;
    }
    if (historyIndex === null) return false;
    if (historyIndex >= prompts.length - 1) {
      setHistoryIndex(null);
      applyEdit({ text: historyStash.current, cursor: historyStash.current.length }, false);
    } else {
      const idx = historyIndex + 1;
      setHistoryIndex(idx);
      applyEdit({ text: prompts[idx], cursor: prompts[idx].length }, false);
    }
    return true;
  };

  // Autocomplete pick: replace the "/<query>" token around the cursor with "/<slug> ".
  const selectSkill = (template: Skill) =>
    applyEdit(replaceSlashToken(message, cursor, template.slug));

  // Palette pick: insert "/<slug> " at the cursor (no token to replace).
  const insertSkillFromPalette = (slug: string) =>
    applyEdit(insertAtCursor(message, cursor, `/${slug} `));

  // External inject (e.g. "reuse this prompt" on a past user message): load the
  // text into the composer with the caret at the end, then clear the channel.
  // Keyed on `nonce` so re-injecting identical text still applies. If the composer
  // already holds different unsent text, confirm first — injecting otherwise
  // silently destroys the in-progress message (and its persisted draft).
  useEffect(() => {
    const { pending } = useComposerStore.getState();
    if (pending == null) return;
    const current = messageRef.current.trim();
    if (current && current !== pending.trim() &&
        !confirm('Replace the text in the composer with this prompt?')) {
      consumeComposer();
      return;
    }
    applyEdit({ text: pending, cursor: pending.length });
    setHistoryIndex(null); // an external inject is a fresh draft, not history recall
    consumeComposer();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerNonce]);

  // Square corner buttons anchored NW (skill) / SE (send) in both variants. The
  // compact square (h-11 = leading-6 + py-2 + border) matches a single-line
  // textarea so the row reads level.
  const square = large ? 'h-12 w-12 px-0 shrink-0' : 'h-11 w-11 px-0 shrink-0';
  const iconSize = large ? 'h-5 w-5' : 'h-4 w-4';

  return (
    <div className={large ? '' : 'px-4 pb-4'}>
      {/* Compact composer floats as a rounded, shadowed card inset from the edges
          (no full-width divider); large keeps the plain start-screen layout. */}
      <div
        className={
          large
            ? ''
            : 'rounded-2xl border border-gray-700 bg-gray-800/90 p-2 shadow-xl shadow-black/40'
        }
      >
        {/* Both variants anchor the skill library to the NW corner and submit to the SE
            corner via top-/bottom-aligned side columns, with the textarea between. */}
        <div className="flex items-stretch gap-2">
        <div className="flex flex-col justify-start">
          <SkillPalette
            onSelect={insertSkillFromPalette}
            disabled={disabled}
            className={square}
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
              setHistoryIndex(null); // a manual edit drops out of history recall
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
              // 6-row floor (min-h) growing to a ~15-row cap (max-h, then scrolls).
              // leading-6 = 1.5rem lines: 6·1.5 + py-2 + border-2 ≈ 10.25rem; 15 ≈ 23.75rem.
              large
                ? 'text-base leading-6 min-h-[10.25rem] max-h-[23.75rem] overflow-y-auto'
                : cn(TEXT_SIZE.body(compact), 'leading-6 max-h-[23.75rem] overflow-y-auto'),
            )}
            rows={large ? 6 : 1}
          />
        </div>
        <div className="flex flex-col justify-end">
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
    </div>
  );
}
