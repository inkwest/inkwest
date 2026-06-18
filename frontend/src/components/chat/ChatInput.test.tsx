import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatInput } from './ChatInput';
import { useDraftsStore } from '@/store/drafts';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { Message, Skill } from '@/types/api';

describe('ChatInput', () => {
  const onSend = vi.fn();

  beforeEach(() => {
    onSend.mockClear();
    useDraftsStore.setState({ drafts: {} });
    useChatStore.setState({ messages: [] });
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
    render(<ChatInput onSend={onSend} />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument();
  });

  it('disables inputs when disabled prop is true', () => {
    render(<ChatInput onSend={onSend} disabled />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeDisabled();
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled();
  });

  it('calls onSend when send button is clicked', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'list pods');
    await user.click(screen.getByRole('button', { name: /send message/i }));

    expect(onSend).toHaveBeenCalledWith('list pods', undefined);
  });

  it('calls onSend on Enter key', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'get nodes{enter}');

    expect(onSend).toHaveBeenCalledWith('get nodes', undefined);
  });

  it('does not send on Shift+Enter', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'line1{shift>}{enter}{/shift}line2');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send empty messages', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    await user.click(screen.getByRole('button', { name: /send message/i }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('clears input after sending', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    await user.type(textarea, 'message{enter}');

    expect(textarea.value).toBe('');
  });

  it('populates initial prompt', () => {
    render(<ChatInput onSend={onSend} initialPrompt="check pods" />);
    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe('check pods');
  });

  it('send button is disabled when textarea is empty', () => {
    render(<ChatInput onSend={onSend} />);
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
    render(<ChatInput onSend={onSend} isStreaming />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'second message{enter}');
    expect(onSend).not.toHaveBeenCalled();
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

    it('shows the animated invite border on a new conversation when empty and unfocused', () => {
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      // autoFocus focuses the composer on mount; the invite only shows once blurred.
      fireEvent.blur(textarea);
      expect(textarea).toHaveClass('invite-border');
    });

    it('drops the invite border on focus, leaving only the static glow', () => {
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      fireEvent.blur(textarea);
      fireEvent.focus(textarea);
      expect(textarea).not.toHaveClass('invite-border');
      expect(textarea.className).toContain('focus:shadow-');
    });

    it('keeps the normal border when unfocused with text present', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      await user.type(textarea, 'draft in progress');
      fireEvent.blur(textarea);
      expect(textarea).not.toHaveClass('invite-border');
    });

    it('keeps the normal border when the conversation already has messages', () => {
      useChatStore.setState({ messages: [existingMessage] });
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      fireEvent.blur(textarea);
      expect(textarea).not.toHaveClass('invite-border');
    });

    it('invites on the start screen even when the store holds a stale session', () => {
      // resetChat only runs in ChatPage, so the start screen can see old messages.
      useChatStore.setState({ messages: [existingMessage] });
      render(<ChatInput onSend={onSend} large />);
      const textarea = getTextarea();
      fireEvent.blur(textarea);
      expect(textarea).toHaveClass('invite-border');
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
      render(<ChatInput onSend={onSend} />);
      await user.type(getTextarea(), '/');
      expect(screen.getByText('/analyse-logs')).toBeInTheDocument();
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
    });

    it('filters the list as the query is typed', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      await user.type(getTextarea(), '/sum');
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
      expect(screen.queryByText('/analyse-logs')).not.toBeInTheDocument();
    });

    it('selects with Enter (inserts "/<slug> ") instead of sending', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      await user.type(textarea, '/analyse{enter}');
      expect(textarea.value).toBe('/analyse-logs ');
      expect(onSend).not.toHaveBeenCalled();
    });

    it('inserts the trigger on click', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      await user.type(textarea, '/sum');
      fireEvent.mouseDown(screen.getByRole('button', { name: /summarize-incident/i }));
      expect(textarea.value).toBe('/summarize-incident ');
    });

    it('closes the dropdown on Escape', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      await user.type(getTextarea(), '/ana{escape}');
      expect(screen.queryByText('/analyse-logs')).not.toBeInTheDocument();
    });

    it('triggers mid-message and replaces the token in place', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      const textarea = getTextarea();
      await user.type(textarea, 'look at /sum');
      expect(screen.getByText('/summarize-incident')).toBeInTheDocument();
      fireEvent.mouseDown(screen.getByRole('button', { name: /summarize-incident/i }));
      expect(textarea.value).toBe('look at /summarize-incident ');
      expect(onSend).not.toHaveBeenCalled();
    });

    it('shows a generic hint line when the skill has no argument_hint', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      await user.type(getTextarea(), '/analyse{enter}'); // selects -> "/analyse-logs "
      expect(screen.getByText(/Inspect pod logs/)).toBeInTheDocument();
      expect(screen.getByText(/arguments libres/)).toBeInTheDocument();
    });

    it('shows the skill-specific argument_hint when present', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={onSend} />);
      await user.type(getTextarea(), '/summarize{enter}'); // -> "/summarize-incident "
      expect(screen.getByText(/\[merge request URL\]/)).toBeInTheDocument(); // bracketed
      expect(screen.queryByText(/arguments libres/)).not.toBeInTheDocument();
    });
  });
});
