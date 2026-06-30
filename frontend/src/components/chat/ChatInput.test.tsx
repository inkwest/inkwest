import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatInput } from './ChatInput';
import { useDraftsStore } from '@/store/drafts';
import { useComposerStore } from '@/store/composer';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { useConnectorsStore } from '@/store/connectors';
import { useMcpStore } from '@/store/mcp';
import { Message, Skill } from '@/types/api';

describe('ChatInput', () => {
  const onSend = vi.fn();

  beforeEach(() => {
    onSend.mockClear();
    useDraftsStore.setState({ drafts: {} });
    useComposerStore.setState({ pending: null, nonce: 0 });
    useChatStore.setState({ messages: [], queuedMessages: [] });
    // A connector is active by default so the baseline send tests don't trip the
    // no-connector confirm modal. The dedicated "no connector" tests below clear it.
    useConnectorsStore.setState({ statuses: { aws: { connector: 'aws', state: 'connected' } } });
    useMcpStore.setState({ enabledBySession: {} });
    // Stub the templates store so the on-mount fetch is a no-op (no real network) and
    // the "/" autocomplete starts empty unless a test populates it.
    useSkillsStore.setState({
      templates: [],
      fetchTemplates: vi.fn(),
      isLoading: false,
      error: null,
    });
  });

  it('renders textarea and send button', () => {
    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument();
  });

  it('disables inputs when disabled prop is true', () => {
    render(<ChatInput onSend={onSend} sessionId="s1" disabled />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeDisabled();
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled();
  });

  it('calls onSend when send button is clicked', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'list pods');
    await user.click(screen.getByRole('button', { name: /send message/i }));

    expect(onSend).toHaveBeenCalledWith('list pods', undefined);
  });

  it('calls onSend on Enter key', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'get nodes{enter}');

    expect(onSend).toHaveBeenCalledWith('get nodes', undefined);
  });

  it('does not send on Shift+Enter', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'line1{shift>}{enter}{/shift}line2');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send on Alt+Enter and inserts a newline', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    await user.type(textarea, 'line1{alt>}{enter}{/alt}line2');

    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe('line1\nline2');
  });

  it('does not send empty messages', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    await user.click(screen.getByRole('button', { name: /send message/i }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('queues the message instead of sending while a turn is streaming (Enter)', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'check the logs{enter}');

    // Not sent (backend rejects a concurrent turn) — buffered and the composer clears.
    expect(onSend).not.toHaveBeenCalled();
    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['check the logs']);
    expect(textarea).toHaveValue('');
    // Surfaced as a removable chip with a live count; no Send button while streaming.
    expect(screen.getByText(/Queued \(1\)/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /send message/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /queue message/i })).toBeInTheDocument();
  });

  it('removes a queued message via its chip', async () => {
    const user = userEvent.setup();
    useChatStore.setState({ queuedMessages: [{ id: 'q1', content: 'drop me' }] });
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    expect(screen.getByText('drop me')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /remove from queue/i }));
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it('edits an unsent queued message inline and commits on Enter', async () => {
    const user = userEvent.setup();
    useChatStore.setState({ queuedMessages: [{ id: 'q1', content: 'old text' }] });
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'old text' }));
    const input = screen.getByRole('textbox', { name: /edit queued message/i });
    await user.clear(input);
    await user.type(input, 'new text{enter}');

    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['new text']);
  });

  it('keeps newlines when editing a multi-line queued message (Shift+Enter)', async () => {
    const user = userEvent.setup();
    useChatStore.setState({ queuedMessages: [{ id: 'q1', content: 'orig' }] });
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'orig' }));
    const input = screen.getByRole('textbox', { name: /edit queued message/i });
    await user.clear(input);
    // Shift+Enter inserts a newline (no commit); a bare Enter then commits.
    await user.type(input, 'line1{shift>}{enter}{/shift}line2{enter}');

    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['line1\nline2']);
  });

  it('discards a queued edit on Escape (keeps the original)', async () => {
    const user = userEvent.setup();
    useChatStore.setState({ queuedMessages: [{ id: 'q1', content: 'keep me' }] });
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'keep me' }));
    const input = screen.getByRole('textbox', { name: /edit queued message/i });
    await user.clear(input);
    await user.type(input, 'changed{escape}');

    expect(useChatStore.getState().queuedMessages.map((m) => m.content)).toEqual(['keep me']);
  });

  it('drops a queued message when edited to empty (no attachments)', async () => {
    const user = userEvent.setup();
    useChatStore.setState({ queuedMessages: [{ id: 'q1', content: 'remove via edit' }] });
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming onStop={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'remove via edit' }));
    const input = screen.getByRole('textbox', { name: /edit queued message/i });
    await user.clear(input);
    await user.type(input, '{enter}');

    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it('clears input after sending', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    await user.type(textarea, 'message{enter}');

    expect(textarea.value).toBe('');
  });

  it('populates initial prompt', () => {
    render(<ChatInput onSend={onSend} sessionId="s1" initialPrompt="check pods" />);
    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe('check pods');
  });

  it('send button is disabled when textarea is empty', () => {
    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled();
  });

  it('restores a per-session draft after unmount/remount', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'investigate crashloop');
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe(
      'investigate crashloop'
    );
  });

  it('does not leak a draft into a different session', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'only for s1');
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s2" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe('');
  });

  it('clears the saved draft after sending', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'send me{enter}');
    expect(onSend).toHaveBeenCalledWith('send me', undefined);
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe('');
  });

  it('does not send on Enter while a response is streaming', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} sessionId="s1" isStreaming />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'second message{enter}');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('stops a streaming response on Escape from anywhere on the page', () => {
    const onStop = vi.fn();
    render(<ChatInput onSend={onSend} sessionId="s1" onStop={onStop} isStreaming />);
    // Dispatched on document (not the composer) — the listener is global.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('does not stop on Escape when no response is streaming', () => {
    const onStop = vi.fn();
    render(<ChatInput onSend={onSend} sessionId="s1" onStop={onStop} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onStop).not.toHaveBeenCalled();
  });

  describe('invite-to-prompt border', () => {
    const existingMessage: Message = {
      id: 'm1',
      session_id: 's1',
      role: 'user',
      content: 'previous question',
      created_at: '2026-01-01T00:00:00Z',
    };

    const getTextarea = () => screen.getByPlaceholderText(/investigate or analyse/i);
    // The focus glow and invite/shimmer border live on the composer card (the
    // borderless textarea sits inside it), so assert against the card wrapper.
    const getCard = () => getTextarea().closest('.rounded-2xl') as HTMLElement;

    it('shows the animated invite border on a new conversation when empty and unfocused', () => {
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      // autoFocus focuses the composer on mount; the invite only shows once blurred.
      fireEvent.blur(getTextarea());
      expect(getCard()).toHaveClass('invite-border');
    });

    it('drops the invite border on focus, leaving only the static glow', () => {
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const textarea = getTextarea();
      fireEvent.blur(textarea);
      fireEvent.focus(textarea);
      expect(getCard()).not.toHaveClass('invite-border');
      expect(getCard().className).toContain('focus-within:shadow-');
    });

    it('keeps the normal border when unfocused with text present', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const textarea = getTextarea();
      await user.type(textarea, 'draft in progress');
      fireEvent.blur(textarea);
      expect(getCard()).not.toHaveClass('invite-border');
    });

    it('keeps the normal border when the conversation already has messages', () => {
      useChatStore.setState({ messages: [existingMessage] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      fireEvent.blur(getTextarea());
      expect(getCard()).not.toHaveClass('invite-border');
    });

    it('invites on the start screen even when the store holds a stale session', () => {
      // resetChat only runs in ChatPage, so the start screen can see old messages.
      useChatStore.setState({ messages: [existingMessage] });
      render(<ChatInput onSend={onSend} sessionId="s1" large />);
      fireEvent.blur(getTextarea());
      expect(getCard()).toHaveClass('invite-border');
    });
  });

  describe('prompt history recall (ArrowUp/ArrowDown)', () => {
    const mkUser = (id: string, content: string): Message => ({
      id,
      session_id: 's1',
      role: 'user',
      content,
      created_at: '2026-01-01T00:00:00Z',
    });
    const getTextarea = () =>
      screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;

    it('recalls the newest prompt on ArrowUp from an empty composer', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'first'), mkUser('m2', 'second')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('second');
    });

    it('walks older then back to newer through history', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'first'), mkUser('m2', 'second')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // second
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // first
      expect(ta.value).toBe('first');
      fireEvent.keyDown(ta, { key: 'ArrowDown' }); // second
      expect(ta.value).toBe('second');
    });

    it('stays on the oldest prompt past the top', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'only')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('only');
    });

    it('restores the live draft when stepping past the newest prompt', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'past')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.change(ta, { target: { value: 'draft' } });
      ta.setSelectionRange(0, 0); // caret at start so ArrowUp enters history
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('past');
      fireEvent.keyDown(ta, { key: 'ArrowDown' });
      expect(ta.value).toBe('draft');
    });

    it('does not enter history when the caret is not at the start', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'past')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.change(ta, { target: { value: 'multi\nline' } });
      ta.setSelectionRange(7, 7); // somewhere on the second line
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('multi\nline');
    });

    it('ignores ArrowDown when not navigating history', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'past')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.change(ta, { target: { value: 'typed' } });
      fireEvent.keyDown(ta, { key: 'ArrowDown' });
      expect(ta.value).toBe('typed');
    });

    it('does nothing on ArrowUp with no past prompts', () => {
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('');
    });

    it('moves the caret within a multi-line recalled prompt before walking older', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'older'), mkUser('m2', 'line1\nline2')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // recall newest multi-line prompt
      expect(ta.value).toBe('line1\nline2');
      // Caret on the last line: ArrowUp moves within the prompt, does not walk older.
      ta.setSelectionRange(ta.value.length, ta.value.length);
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('line1\nline2');
      // Caret on the first line: now ArrowUp walks to the older prompt.
      ta.setSelectionRange(0, 0);
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('older');
    });

    it('collapses consecutive duplicate prompts', () => {
      useChatStore.setState({
        messages: [mkUser('m1', 'dup'), mkUser('m2', 'dup'), mkUser('m3', 'other')],
      });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // newest: 'other'
      expect(ta.value).toBe('other');
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // 'dup' once, not twice
      expect(ta.value).toBe('dup');
      fireEvent.keyDown(ta, { key: 'ArrowUp' }); // already oldest
      expect(ta.value).toBe('dup');
    });

    it('recalls from caret 0 even when the draft begins with a newline', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'past')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.change(ta, { target: { value: '\nleading newline' } });
      ta.setSelectionRange(0, 0); // caret on the empty first line
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('past');
    });

    it('keeps the persisted draft intact while recalling (only the textarea changes)', () => {
      useChatStore.setState({ messages: [mkUser('m1', 'past')] });
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const ta = getTextarea();
      fireEvent.change(ta, { target: { value: 'mydraft' } });
      ta.setSelectionRange(0, 0);
      fireEvent.keyDown(ta, { key: 'ArrowUp' });
      expect(ta.value).toBe('past');
      // The recall must not clobber the user's own draft in the store.
      expect(useDraftsStore.getState().drafts['s1']).toBe('mydraft');
    });
  });

  describe('attachment handoff auto-submit', () => {
    const att = [{ filename: 'a.png', original_name: 'a.png', content_type: 'image/png', size: 10 }];

    it('auto-submits seeded attachments even with no text', async () => {
      render(<ChatInput onSend={onSend} sessionId="s1" autoSubmit initialAttachments={att} />);
      await waitFor(() => expect(onSend).toHaveBeenCalledWith('', att));
    });

    it('auto-submits a seeded prompt together with its attachments', async () => {
      render(
        <ChatInput onSend={onSend} sessionId="s1" autoSubmit initialPrompt="hello" initialAttachments={att} />,
      );
      await waitFor(() => expect(onSend).toHaveBeenCalledWith('hello', att));
    });

    it('does not auto-submit when there is neither text nor attachments', async () => {
      render(<ChatInput onSend={onSend} sessionId="s1" autoSubmit />);
      await new Promise((r) => setTimeout(r, 50));
      expect(onSend).not.toHaveBeenCalled();
    });
  });

  describe('skill autocomplete ("/")', () => {
    const templates: Skill[] = [
      {
        id: '1',
        name: 'Analyse Logs',
        slug: 'analyse-logs',
        content: 'x',
        description: 'Inspect pod logs',
        is_active: true,
        created_at: '2026-01-01T00:00:00Z',
      },
      {
        id: '2',
        name: 'Summarize Incident',
        slug: 'summarize-incident',
        argument_hint: 'merge request URL',
        content: 'y',
        description: 'Write a summary',
        is_active: true,
        created_at: '2026-01-01T00:00:00Z',
      },
    ];
    const getTextarea = () =>
      screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;

    beforeEach(() => {
      useSkillsStore.setState({ templates, fetchTemplates: vi.fn() });
    });

    it('opens the dropdown listing skills when the message starts with "/"', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), '/');
      expect(screen.getByText('/analyse-logs')).toBeInTheDocument();
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
    });

    it('filters the list as the query is typed', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), '/sum');
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
      expect(screen.queryByText('/analyse-logs')).not.toBeInTheDocument();
    });

    it('selects with Enter (inserts "/<slug> ") instead of sending', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const textarea = getTextarea();
      await user.type(textarea, '/analyse{enter}');
      expect(textarea.value).toBe('/analyse-logs ');
      expect(onSend).not.toHaveBeenCalled();
    });

    it('inserts the trigger on click', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const textarea = getTextarea();
      await user.type(textarea, '/sum');
      fireEvent.mouseDown(screen.getByRole('button', { name: /summarize-incident/i }));
      expect(textarea.value).toBe('/summarize-incident ');
    });

    it('closes the dropdown on Escape', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), '/ana{escape}');
      expect(screen.queryByText('/analyse-logs')).not.toBeInTheDocument();
    });

    it('triggers mid-message and replaces the token in place', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      const textarea = getTextarea();
      await user.type(textarea, 'look at /sum');
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
      fireEvent.mouseDown(screen.getByRole('button', { name: /summarize-incident/i }));
      expect(textarea.value).toBe('look at /summarize-incident ');
      expect(onSend).not.toHaveBeenCalled();
    });

    it('shows a generic hint line when the skill has no argument_hint', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), '/analyse{enter}'); // selects -> "/analyse-logs "
      expect(screen.getByText(/Inspect pod logs/)).toBeInTheDocument();
      expect(screen.getByText(/arguments libres/)).toBeInTheDocument();
    });

    it('shows the skill-specific argument_hint when present', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), '/summarize{enter}'); // -> "/summarize-incident "
      expect(screen.getByText(/\[merge request URL\]/)).toBeInTheDocument(); // bracketed
      expect(screen.queryByText(/arguments libres/)).not.toBeInTheDocument();
    });
  });

  describe('composer inject (reuse prompt)', () => {
    const getTextarea = () =>
      screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;

    it('loads injected text into an empty composer without confirming', () => {
      const confirmSpy = vi.spyOn(window, 'confirm');
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      act(() => useComposerStore.getState().inject('reuse this prompt'));
      expect(getTextarea().value).toBe('reuse this prompt');
      expect(confirmSpy).not.toHaveBeenCalled();
      // Channel is consumed so a remount can't re-inject.
      expect(useComposerStore.getState().pending).toBeNull();
      confirmSpy.mockRestore();
    });

    it('overwrites non-empty composer text after the user confirms', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), 'half-typed draft');
      act(() => useComposerStore.getState().inject('reused'));
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(getTextarea().value).toBe('reused');
      confirmSpy.mockRestore();
    });

    it('keeps the existing draft when the user cancels the overwrite', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), 'half-typed draft');
      act(() => useComposerStore.getState().inject('reused'));
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(getTextarea().value).toBe('half-typed draft');
      // Channel is consumed regardless, so the cancelled prompt won't linger.
      expect(useComposerStore.getState().pending).toBeNull();
      confirmSpy.mockRestore();
    });

    it('does not confirm when the injected text matches the current draft', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm');
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(getTextarea(), 'same text');
      act(() => useComposerStore.getState().inject('same text'));
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(getTextarea().value).toBe('same text');
      confirmSpy.mockRestore();
    });
  });

  describe('no-connector guard', () => {
    const noConnectors = () => useConnectorsStore.setState({ statuses: {} });

    it('shows no warning when a connector is active, even with text typed', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'hello');
      expect(screen.queryByRole('img', { name: /no connector activated/i })).not.toBeInTheDocument();
    });

    it('hides the warning when no connector but the box is empty', () => {
      noConnectors();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      expect(screen.queryByRole('img', { name: /no connector activated/i })).not.toBeInTheDocument();
    });

    it('shows the warning once the user types with no connector active', async () => {
      noConnectors();
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'hello');
      expect(screen.getByRole('img', { name: /no connector activated/i })).toBeInTheDocument();
    });

    it('opens the confirm modal instead of sending when no connector is active', async () => {
      noConnectors();
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'list pods{enter}');
      expect(onSend).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /send anyway/i })).toBeInTheDocument();
    });

    it('sends after confirming "Send anyway"', async () => {
      noConnectors();
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'list pods{enter}');
      await user.click(screen.getByRole('button', { name: /send anyway/i }));
      expect(onSend).toHaveBeenCalledWith('list pods', undefined);
    });

    it('does not send when the confirm modal is cancelled', async () => {
      noConnectors();
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'list pods{enter}');
      await user.click(screen.getByRole('button', { name: /cancel/i }));
      expect(onSend).not.toHaveBeenCalled();
      // The typed text is preserved so the user can grant access and retry.
      expect(screen.getByPlaceholderText(/investigate or analyse/i)).toHaveValue('list pods');
    });

    it('counts an enabled MCP server as an active connector (no warning)', async () => {
      useConnectorsStore.setState({ statuses: {} });
      useMcpStore.setState({ enabledBySession: { s1: ['coralogix'] } });
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} sessionId="s1" />);
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'hello');
      expect(screen.queryByRole('img', { name: /no connector activated/i })).not.toBeInTheDocument();
    });

    it('ignores the global connector store pre-session (start screen)', async () => {
      // A connector left "connected" by a previously-open session must not suppress the
      // warning on the start screen, where there is no session and thus no grants.
      useConnectorsStore.setState({ statuses: { aws: { connector: 'aws', state: 'connected' } } });
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />); // no sessionId
      await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'hello');
      expect(screen.getByRole('img', { name: /no connector activated/i })).toBeInTheDocument();
    });

    it('glows the connector controls while the warning is focused, and stops when emptied', async () => {
      noConnectors();
      const user = userEvent.setup();
      render(
        <ChatInput onSend={onSend} sessionId="s1" connectorControls={<button>conn</button>} />,
      );
      const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
      await user.type(textarea, 'hello');
      const wrap = screen.getByTestId('connector-controls-wrap');
      expect(wrap.className).not.toMatch(/ring-2/);
      await user.click(screen.getByRole('img', { name: /no connector activated/i }));
      expect(wrap.className).toMatch(/ring-2/);
      // Clearing the box hides the warning; the glow must not outlive it (stuck-glow guard).
      await user.clear(textarea);
      expect(screen.getByTestId('connector-controls-wrap').className).not.toMatch(/ring-2/);
    });
  });
});
