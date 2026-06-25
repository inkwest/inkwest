import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef, type KeyboardEvent as ReactKeyboardEvent, type ChangeEvent, type ClipboardEvent, type ReactNode } from 'react';
import { Send, Square, Paperclip, Loader2 } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';
import { useDraftsStore } from '@/store/drafts';
import { useComposerStore } from '@/store/composer';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { useConfigStore } from '@/store/config';
import { Skill, Attachment } from '@/types/api';
import { uploadAttachments, deleteAttachment } from '@/services/api';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { SkillPalette } from './SkillPalette';
import { ToolHoverCard } from './ToolHoverCard';
import { SkillAutocomplete } from './SkillAutocomplete';
import { AttachmentChips, PendingAttachment } from './Attachments';
import { formatArgs } from '@/lib/skillArgs';

interface Props {
  onSend: (message: string, attachments?: Attachment[]) => void;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  initialPrompt?: string | null;
  // Attachments handed off from the start screen (already uploaded to this session),
  // seeded into the composer once so the auto-submit includes them.
  initialAttachments?: Attachment[] | null;
  // Submit initialPrompt automatically once the composer is enabled (WS connected).
  // The text stays in the textarea until then, so it's recoverable if connect fails.
  autoSubmit?: boolean;
  sessionId?: string;
  // Centered "start a conversation" variant: taller textarea, no top divider.
  large?: boolean;
  placeholder?: string;
  // Optional model/engine control rendered inline in the toolbar (left of send).
  // Supplied by the caller (SessionModelSelect in chat, ModelEnginePicker on the
  // start screen) so ChatInput stays presentational and store-agnostic.
  modelControl?: ReactNode;
  // Optional external-access controls (the per-connector icon buttons) rendered in
  // the left toolbar cluster, after attach/skills. Caller-supplied for the same reason.
  connectorControls?: ReactNode;
  // Lazily materialize the session before attaching, on the start screen where no
  // session exists yet (mirrors the connector "Give access" flow). When provided,
  // the attach button + paste show pre-session. MUST resolve to the new session id so
  // the upload can target it without waiting on a re-render of the `sessionId` prop.
  onBeforeAttach?: () => Promise<string | void>;
  // Broadcast typing state to other participants (owner only); no-op when omitted.
  onTyping?: (state: boolean) => void;
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

export function ChatInput({ onSend, onStop, disabled, isStreaming, initialPrompt, initialAttachments, autoSubmit, sessionId, large, placeholder, modelControl, connectorControls, onBeforeAttach, onTyping }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);
  // Debounced typing signal: emit `true` on edit, then `false` after a short idle
  // (also cleared on send/unmount) so other participants see "X is typing…".
  const typingTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const signalTyping = useCallback(() => {
    if (!onTyping) return;
    onTyping(true);
    if (typingTimer.current) clearTimeout(typingTimer.current);
    typingTimer.current = setTimeout(() => onTyping(false), 2500);
  }, [onTyping]);
  const stopTyping = useCallback(() => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    onTyping?.(false);
  }, [onTyping]);
  useEffect(() => () => stopTyping(), [stopTyping]);
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

  // --- File attachments (sandbox/claude-cli only; needs a session to upload to) ---
  const attachmentsEnabled = useConfigStore((s) => s.config?.attachments_enabled ?? false);
  // Available once there's a session, or pre-session when the caller can lazily make
  // one (start screen). Both the attach button and paste lazily create the session
  // via onBeforeAttach when none exists yet (see ensureAttachSession).
  const canAttach = attachmentsEnabled && (!!sessionId || !!onBeforeAttach);
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

  // Seed attachments handed off from the start screen (already uploaded to this
  // session) so the auto-submit sends them. One-shot; no previewUrl (the local File
  // didn't survive the navigation — the chip shows the filename only).
  const initialAttachmentsApplied = useRef(false);
  useEffect(() => {
    if (!initialAttachments?.length || initialAttachmentsApplied.current) return;
    initialAttachmentsApplied.current = true;
    setAttachments(initialAttachments.map((a) => ({ ...a })));
  }, [initialAttachments]);

  const addFiles = useCallback(
    // sid defaults to the current session; callers that just lazily created one pass it
    // explicitly so the upload isn't blocked by the not-yet-propagated `sessionId` prop.
    async (files: File[], sid: string | undefined = sessionId) => {
      if (!sid || files.length === 0) return;
      setAttachError(null);
      setUploading(true);
      try {
        // Descriptors come back in input order, so we pair each with its local
        // File to build an image preview without a round-trip to the server.
        const uploaded = await uploadAttachments(sid, files);
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

  // The session to upload into: the current one, or a freshly lazily-created one
  // (start screen). Returns undefined if neither is possible.
  const ensureAttachSession = useCallback(async (): Promise<string | undefined> => {
    if (sessionId) return sessionId;
    if (onBeforeAttach) return (await onBeforeAttach()) || undefined;
    return undefined;
  }, [sessionId, onBeforeAttach]);

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canAttach) return;
    const files = Array.from(e.clipboardData.files);
    if (files.length === 0) return;
    // preventDefault + snapshot the files synchronously: after an await the paste would
    // already have landed in the textarea and clipboardData would be cleared.
    e.preventDefault(); // a pasted file shouldn't also dump a path/text into the box
    void (async () => {
      const sid = await ensureAttachSession();
      if (sid) void addFiles(files, sid);
    })();
  };

  // The session resolved when the picker was opened, captured so handleBrowse uploads
  // to the freshly lazily-created session without depending on the `sessionId` prop
  // having re-rendered by the time the user picks a file.
  const pendingAttachSidRef = useRef<string | undefined>(undefined);

  const handleBrowse = (e: ChangeEvent<HTMLInputElement>) => {
    const sid = pendingAttachSidRef.current ?? sessionId;
    pendingAttachSidRef.current = undefined;
    void addFiles(Array.from(e.target.files ?? []), sid);
    e.target.value = ''; // let the same file be re-picked after a removal
  };

  // Open the native file picker, lazily creating the session first when none exists
  // yet (start screen). The resolved id is stashed for handleBrowse (above).
  const openFilePicker = useCallback(async () => {
    pendingAttachSidRef.current = await ensureAttachSession();
    fileInputRef.current?.click();
  }, [ensureAttachSession]);

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
    // Add back any vertical border (offsetHeight includes it, scrollHeight/clientHeight
    // don't). The textarea is borderless today so this is 0, but deriving it keeps the
    // sizing correct if a border is ever reintroduced rather than silently clipping.
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
      stopTyping();
      setMessage('');
      attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
      setAttachments([]);
      setAttachError(null);
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
    // Don't latch the one-shot while an upload is in flight: handleSend would no-op on
    // `uploading` and we'd never retry. `uploading` is in the deps so this re-fires once
    // the upload settles.
    if (uploading) return;
    // Fire only for the untouched handoff: the composer text equals the seeded prompt
    // (empty when only attachments were handed off — attachments.length in deps so the
    // seed effect re-triggers this), and there is actually something to send.
    const seededText = initialPrompt?.trim() ?? '';
    if (message.trim() !== seededText) return;
    if (!seededText && attachments.length === 0) return;
    autoSubmittedRef.current = true;
    handleSend();
    // handleSend reads current message/attachments/disabled; this is a guarded one-shot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSubmit, disabled, isStreaming, uploading, message, initialPrompt, attachments.length]);

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
    // Alt+Enter inserts a newline, like Shift+Enter. Shift+Enter relies on the
    // textarea's built-in default; Alt+Enter has no such default, so splice the
    // newline in at the caret (replacing any selection, matching how typing behaves).
    if (e.key === 'Enter' && e.altKey) {
      e.preventDefault();
      const { selectionStart: start, selectionEnd: end, value } = ta;
      applyEdit({ text: value.slice(0, start) + '\n' + value.slice(end), cursor: start + 1 });
      return;
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

  // Toolbar buttons are fixed small squares now (not full-height side bars): the
  // textarea owns the vertical space and the controls sit on one row beneath it.
  // Kept h-9 in both variants so the caller-supplied connector icons (also h-9) line
  // up with attach/skills/send across the row.
  const btnSize = 'h-9 w-9';
  const toolBtn = cn(
    'flex items-center justify-center rounded-lg border border-gray-600 bg-gray-700 text-gray-300 hover:bg-gray-600 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed shrink-0',
    btnSize,
  );
  const iconSize = 'h-4 w-4';

  return (
    <div className={large ? '' : 'px-4 pb-4'}>
      {/* Single rounded card: textarea on top, one control row beneath (attach /
          skills on the left, model picker + send on the right). The focus glow
          lives on the card so the borderless textarea inside reads as one field. */}
      <div
        className={cn(
          'flex flex-col rounded-2xl border border-gray-700 bg-gray-800/70 backdrop-blur-xl p-2 shadow-xl shadow-black/40 transition-colors',
          'focus-within:border-blue-500 focus-within:shadow-[0_0_20px_3px_rgba(59,130,246,0.45)]',
          shimmer && message.trim() && 'shimmer-border',
          inviteToPrompt && 'invite-border',
        )}
      >
        {/* Attached files (sandbox/claude-cli only), rattachés au message en cours. */}
        {canAttach && <AttachmentChips attachments={attachments} onRemove={removeAttachment} />}
        {attachError && <div className="mb-2 px-1 text-xs text-red-400">{attachError}</div>}
        <div className="relative">
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
              signalTyping();
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
              'w-full bg-transparent border-0 px-2 py-1.5 text-white placeholder-gray-500 resize-none focus:outline-none disabled:opacity-50',
              // 6-row floor (min-h) growing to a ~15-row cap (max-h, then scrolls).
              // leading-6 = 1.5rem lines: 6·1.5 + py ≈ 9.75rem; 15 ≈ 23.25rem.
              large
                ? 'text-base leading-6 min-h-[9.75rem] max-h-[23.25rem] overflow-y-auto'
                : cn(TEXT_SIZE.body(compact), 'leading-6 max-h-[23.25rem] overflow-y-auto'),
            )}
            rows={large ? 6 : 1}
          />
        </div>
        {/* Control row */}
        <div className="mt-1 flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5">
            {canAttach && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={handleBrowse}
                  aria-hidden="true"
                  tabIndex={-1}
                />
                <ToolHoverCard
                  title="Attach files"
                  body={
                    <p className="text-gray-400">
                      Add files to this message{attachments.length > 0 ? ` (${attachments.length} attached)` : ''}.
                    </p>
                  }
                >
                  <button
                    type="button"
                    onClick={() => void openFilePicker()}
                    disabled={disabled || uploading}
                    aria-label="Attach files"
                    title="Attach files"
                    data-tour="attach"
                    className={toolBtn}
                  >
                    {uploading ? (
                      <Loader2 className={cn(iconSize, 'animate-spin')} />
                    ) : (
                      <Paperclip className={iconSize} />
                    )}
                  </button>
                </ToolHoverCard>
              </>
            )}
            <ToolHoverCard
              title="Skill library"
              body={<p className="text-gray-400">Insert a saved skill, or type / in the message.</p>}
            >
              <SkillPalette
                onSelect={insertSkillFromPalette}
                disabled={disabled}
                className={cn(toolBtn, 'px-0')}
              />
            </ToolHoverCard>
            {connectorControls}
          </div>
          <div className="flex items-center gap-2">
            {modelControl}
            {isStreaming ? (
              <button
                onClick={onStop}
                aria-label="Stop"
                className={cn(
                  'flex items-center justify-center rounded-lg border border-transparent bg-red-600 hover:bg-red-700 shrink-0',
                  btnSize,
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
                  'flex items-center justify-center rounded-lg border border-transparent bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed shrink-0',
                  btnSize,
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
