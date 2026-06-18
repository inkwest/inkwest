import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef, KeyboardEvent, ChangeEvent, ClipboardEvent } from 'react';
import { Send, Square, Paperclip, Loader2 } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { useDraftsStore } from '@/store/drafts';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { useConfigStore } from '@/store/config';
import { Skill, Attachment } from '@/types/api';
import { uploadAttachments, deleteAttachment } from '@/services/api';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { SkillPalette } from './SkillPalette';
import { SkillAutocomplete } from './SkillAutocomplete';
import { AttachmentChips, PendingAttachment } from './Attachments';
import { formatArgs } from '@/lib/skillArgs';

interface Props {
  onSend: (message: string, attachments?: Attachment[]) => void;
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

  // --- File attachments (sandbox/claude-cli only; needs a session to upload to) ---
  const attachmentsEnabled = useConfigStore((s) => s.config?.attachments_enabled ?? false);
  const canAttach = !!sessionId && attachmentsEnabled;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);

  // Revoke any object URLs still held when the composer unmounts (session switch).
  useEffect(() => {
    return () => {
      attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const addFiles = useCallback(
    async (files: File[]) => {
      if (!sessionId || files.length === 0) return;
      setAttachError(null);
      setUploading(true);
      try {
        // Descriptors come back in input order, so we pair each with its local
        // File to build an image preview without a round-trip to the server.
        const uploaded = await uploadAttachments(sessionId, files);
        const withPreview: PendingAttachment[] = uploaded.map((d, i) => ({
          ...d,
          previewUrl:
            files[i] && d.content_type.startsWith('image/')
              ? URL.createObjectURL(files[i])
              : undefined,
        }));
        setAttachments((prev) => [...prev, ...withPreview]);
      } catch (e) {
        setAttachError(e instanceof Error ? e.message : 'Upload failed');
      } finally {
        setUploading(false);
      }
    },
    [sessionId],
  );

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canAttach) return;
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault(); // a pasted file shouldn't also dump a path/text into the box
      void addFiles(files);
    }
  };

  const handleBrowse = (e: ChangeEvent<HTMLInputElement>) => {
    void addFiles(Array.from(e.target.files ?? []));
    e.target.value = ''; // let the same file be re-picked after a removal
  };

  const removeAttachment = (filename: string) => {
    setAttachments((prev) => {
      const gone = prev.find((a) => a.filename === filename);
      if (gone?.previewUrl) URL.revokeObjectURL(gone.previewUrl);
      return prev.filter((a) => a.filename !== filename);
    });
    if (sessionId) {
      void deleteAttachment(sessionId, filename).catch(() => {
        /* best-effort: the file is dropped client-side regardless */
      });
    }
  };

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
    const text = message.trim();
    // Send when there's text OR attachments. Don't send while a response is
    // streaming (the button is Stop then, but Enter would still fire and the
    // backend rejects a second turn) or while an upload is still in flight (its
    // descriptor wouldn't be included).
    if ((text || attachments.length > 0) && !disabled && !isStreaming && !uploading) {
      // Strip the client-only previewUrl from the wire payload.
      const descriptors: Attachment[] = attachments.map((a) => ({
        filename: a.filename,
        original_name: a.original_name,
        content_type: a.content_type,
        size: a.size,
      }));
      onSend(text, descriptors.length ? descriptors : undefined);
      setMessage('');
      attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
      setAttachments([]);
      setAttachError(null);
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

  // Fixed-width side buttons (skill / attach / send) that stretch to the full
  // textarea height via the row's `items-stretch`: a single line reads level, and
  // a multi-line message grows them into tall bars instead of small squares.
  const sideBtn = large ? 'h-full w-12 px-0 shrink-0' : 'h-full w-11 px-0 shrink-0';
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
        {/* Attached files (sandbox/claude-cli only), rattachés au message en cours. */}
        {canAttach && <AttachmentChips attachments={attachments} onRemove={removeAttachment} />}
        {attachError && <div className="mb-2 px-1 text-xs text-red-400">{attachError}</div>}
        {/* Both variants anchor the skill library to the NW corner and submit to the SE
            corner via top-/bottom-aligned side columns, with the textarea between. */}
        <div className="flex items-stretch gap-2">
        <div className="flex flex-col justify-start">
          <SkillPalette
            onSelect={insertSkillFromPalette}
            disabled={disabled}
            className={sideBtn}
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
            onPaste={handlePaste}
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
        {canAttach && (
          <div className="flex flex-col justify-end">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={handleBrowse}
              aria-hidden="true"
              tabIndex={-1}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled || uploading}
              aria-label="Attach files"
              title="Attach files"
              className={cn(
                'flex items-center justify-center rounded-lg border border-gray-600 bg-gray-700 text-gray-300 hover:bg-gray-600 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed',
                sideBtn,
              )}
            >
              {uploading ? (
                <Loader2 className={cn(iconSize, 'animate-spin')} />
              ) : (
                <Paperclip className={iconSize} />
              )}
            </button>
          </div>
        )}
        <div className="flex flex-col justify-end">
          {isStreaming ? (
            <button
              onClick={onStop}
              aria-label="Stop"
              className={cn(
                'flex items-center justify-center rounded-lg border border-transparent bg-red-600 hover:bg-red-700',
                sideBtn,
              )}
            >
              <Square className={iconSize} />
            </button>
          ) : (
            <button
              onClick={handleSend}
              aria-label="Send message"
              disabled={disabled || uploading || (!message.trim() && attachments.length === 0)}
              className={cn(
                'flex items-center justify-center rounded-lg border border-transparent bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed',
                sideBtn,
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
