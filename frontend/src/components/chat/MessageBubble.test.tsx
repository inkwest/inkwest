import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MessageBubble } from './MessageBubble';
import type { Message } from '@/types/api';
import { useSettingsStore } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';
import { useComposerStore } from '@/store/composer';
import { formatTimestamp, formatFullTimestamp } from '@/lib/utils';

vi.mock('./MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}));

vi.mock('./ToolCallCard', () => ({
  ToolCallCard: ({ name }: { name: string }) => <div data-testid="tool-card">{name}</div>,
}));

const baseMessage: Message = {
  id: 'msg-1',
  session_id: 'sess-1',
  role: 'user',
  content: 'What pods are running?',
  created_at: '2026-06-11T14:30:00Z',
};

describe('MessageBubble', () => {
  beforeEach(() => {
    useSettingsStore.setState({ showTimestamps: false });
  });

  it('renders user message with text content', () => {
    render(<MessageBubble message={baseMessage} />);
    expect(screen.getByTestId('markdown')).toHaveTextContent('What pods are running?');
  });

  it('renders assistant message', () => {
    const msg: Message = { ...baseMessage, role: 'assistant', content: 'Here are your pods.' };
    render(<MessageBubble message={msg} />);
    expect(screen.getByTestId('markdown')).toHaveTextContent('Here are your pods.');
  });

  it('exposes a message anchor id for search deep-linking', () => {
    const { container } = render(<MessageBubble message={baseMessage} />);
    expect(container.querySelector('#message-msg-1')).toBeInTheDocument();
  });

  it('exposes scroll anchors for rounds fused into the bubble', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'assistant',
      content: 'Done.',
      mergedIds: ['round-2', 'round-3'],
    };
    const { container } = render(<MessageBubble message={msg} />);
    // Lead id plus one anchor per absorbed round, so a search deep-link resolves.
    expect(container.querySelector('#message-msg-1')).toBeInTheDocument();
    expect(container.querySelector('#message-round-2')).toBeInTheDocument();
    expect(container.querySelector('#message-round-3')).toBeInTheDocument();
  });

  it('applies the highlight ring only when highlighted', () => {
    const { container, rerender } = render(<MessageBubble message={baseMessage} />);
    expect(container.querySelector('.ring-2')).toBeNull();
    rerender(<MessageBubble message={baseMessage} highlighted />);
    expect(container.querySelector('.ring-2')).toBeInTheDocument();
  });

  it('renders content blocks with tool use', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'assistant',
      content: [
        { type: 'text', text: 'Checking pods' },
        { type: 'tool_use', name: 'kubectl_get', input: { resource: 'pods' } },
      ],
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByTestId('markdown')).toHaveTextContent('Checking pods');
    expect(screen.getByTestId('tool-card')).toHaveTextContent('kubectl_get');
  });

  it('renders tool result block', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'assistant',
      content: [
        { type: 'tool_result', content: 'pod1 Running', is_error: false },
      ],
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByTestId('tool-card')).toHaveTextContent('Result');
  });

  it('shows error state', () => {
    const msg: Message = {
      ...baseMessage,
      status: 'error',
      error_message: 'API timeout',
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText('API timeout')).toBeInTheDocument();
  });

  it('shows retry button when retryable', () => {
    const msg: Message = {
      ...baseMessage,
      status: 'error',
      error_message: 'Failed',
      is_retryable: true,
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/retry/i)).toBeInTheDocument();
  });

  it('fires onRetry with the message id when the retry button is clicked', () => {
    const onRetry = vi.fn();
    const msg: Message = {
      ...baseMessage,
      status: 'error',
      error_message: 'Failed',
      is_retryable: true,
    };
    render(<MessageBubble message={msg} onRetry={onRetry} />);
    fireEvent.click(screen.getByText(/retry/i));
    expect(onRetry).toHaveBeenCalledWith('msg-1');
  });

  it('hides the timestamp when showTimestamps is disabled', () => {
    useSettingsStore.setState({ showTimestamps: false });
    render(<MessageBubble message={baseMessage} />);
    expect(screen.queryByText(formatTimestamp(baseMessage.created_at))).not.toBeInTheDocument();
  });

  it("renders today's timestamp as 24h HH:MM:SS (no date prefix) when enabled", () => {
    useSettingsStore.setState({ showTimestamps: true });
    const today = new Date();
    today.setHours(14, 30, 15, 0);
    const msg: Message = { ...baseMessage, created_at: today.toISOString() };
    render(<MessageBubble message={msg} />);
    const expected = formatTimestamp(msg.created_at);
    expect(expected).toMatch(/^\d{2}:\d{2}:\d{2}$/); // HH:MM:SS only, no date
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it('prefixes the ISO date (YYYY-MM-DD HH:MM:SS) for messages not from today', () => {
    useSettingsStore.setState({ showTimestamps: true });
    render(<MessageBubble message={baseMessage} />); // baseMessage is dated 2026-06-11
    const expected = formatTimestamp(baseMessage.created_at);
    expect(expected).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it('always exposes the full date+seconds as the hover overlay (title)', () => {
    useSettingsStore.setState({ showTimestamps: true });
    render(<MessageBubble message={baseMessage} />);
    const full = formatFullTimestamp(baseMessage.created_at);
    expect(full).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
    expect(screen.getByText(formatTimestamp(baseMessage.created_at))).toHaveAttribute('title', full);
  });

  it('renders an AWS grant by account name/role and keeps the id out of the line', () => {
    useSettingsStore.setState({ showTimestamps: true });
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: {
        type: 'session_event',
        subject: 'aws',
        action: 'granted',
        targets: [
          { account_id: '111122223333', account_name: 'acme-prod', role: 'ReadOnly' },
          { account_id: '444455556666', account_name: 'acme-dev', role: 'Admin' },
        ],
      },
    };
    render(<MessageBubble message={msg} />);
    expect(
      screen.getByText(/AWS access granted to: acme-prod\/ReadOnly and acme-dev\/Admin/),
    ).toBeInTheDocument();
    // The account id is preserved in the data but hidden from the rendered line.
    expect(screen.queryByText(/111122223333/)).not.toBeInTheDocument();
    expect(screen.getByText(formatTimestamp(msg.created_at))).toBeInTheDocument();
    expect(screen.queryByTestId('markdown')).not.toBeInTheDocument(); // a note, not a bubble
  });

  it('renders a batched MCP connect event with a natural target list', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: {
        type: 'session_event',
        subject: 'mcp',
        action: 'connected',
        targets: [{ server: 'coralogix' }, { server: 'sentry' }],
      },
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/MCP connected to: coralogix and sentry/)).toBeInTheDocument();
  });

  it('renders an MCP disconnect event with "from"', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: { type: 'session_event', subject: 'mcp', action: 'disconnected', targets: [{ server: 'sentry' }] },
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/MCP disconnected from: sentry/)).toBeInTheDocument();
  });

  it('renders a model change event', () => {
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: { type: 'session_event', subject: 'model', action: 'changed', targets: [{ model: 'claude-opus-4-8' }] },
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/Model changed to claude-opus-4-8/)).toBeInTheDocument();
  });

  it('truncates more than 5 targets to "first 5 … and N more" and expands on click', () => {
    const targets = Array.from({ length: 17 }, (_, i) => ({ server: `srv-${i + 1}` }));
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: { type: 'session_event', subject: 'mcp', action: 'connected', targets },
    };
    render(<MessageBubble message={msg} />);
    // Collapsed: first 5 shown, rest behind an expander; srv-6+ not rendered yet.
    expect(screen.getByText(/srv-1, srv-2, srv-3, srv-4, srv-5 and/)).toBeInTheDocument();
    expect(screen.queryByText(/srv-17/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /12 more/ }));
    // Expanded: full list + a "show less" toggle.
    expect(screen.getByText(/srv-16 and srv-17/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /more/ })).not.toBeInTheDocument();
    // Collapses back: srv-17 hidden again, "N more" returns.
    fireEvent.click(screen.getByRole('button', { name: /show less/ }));
    expect(screen.queryByText(/srv-17/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /12 more/ })).toBeInTheDocument();
  });

  it('falls back to a plain string for a system message without structured content', () => {
    const msg: Message = { ...baseMessage, role: 'system', content: 'GitLab access renewed' };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/GitLab access renewed/)).toBeInTheDocument();
  });

  describe('copy as Markdown', () => {
    beforeEach(() => {
      useAuthStore.setState({
        user: { id: 'u1', email: 'd@x', display_name: 'Damien Degois', is_admin: false, roles: [] } as never,
      });
      useConfigStore.setState({
        config: {
          model: 'claude-opus-4-8',
          available_models: [
            { id: 'claude-opus-4-8', label: 'Claude Opus 4.8' },
            { id: 'claude-haiku-4-5', label: 'Claude Haiku 4.5' },
          ],
        } as never,
      });
    });

    it('copies the assistant message as Markdown with an attribution footer', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      const msg: Message = { ...baseMessage, role: 'assistant', content: 'Here are your pods.' };
      render(<MessageBubble message={msg} />);

      // "Copy as Markdown" lives in the ellipsis menu.
      fireEvent.click(screen.getByRole('button', { name: /more copy options/i }));
      fireEvent.click(screen.getByRole('menuitem', { name: /copy as markdown/i }));

      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      const copied = writeText.mock.calls[0][0] as string;
      expect(copied).toContain('Here are your pods.');
      expect(copied).toContain('Powered by Inkwest');
      expect(copied).toContain('Conversation with Damien Degois');
      expect(copied).toContain('Claude Opus 4.8');
      expect(copied).toContain('conversation: sess-1 · message: msg-1');
      vi.unstubAllGlobals();
    });

    it("uses the message's own model over the session default in the footer", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      // Session default is Opus, but this turn ran on Haiku — the footer must reflect the turn.
      const msg: Message = { ...baseMessage, role: 'assistant', content: 'Answer.', model: 'claude-haiku-4-5' };
      render(<MessageBubble message={msg} />);

      fireEvent.click(screen.getByRole('button', { name: /more copy options/i }));
      fireEvent.click(screen.getByRole('menuitem', { name: /copy as markdown/i }));

      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      const copied = writeText.mock.calls[0][0] as string;
      expect(copied).toContain('Claude Haiku 4.5');
      expect(copied).not.toContain('Claude Opus 4.8');
      vi.unstubAllGlobals();
    });

    it('includes the engine label in the footer (session pin over deployment default)', async () => {
      useConfigStore.setState({
        config: {
          model: 'claude-opus-4-8',
          default_engine: 'claude-cli',
          available_models: [{ id: 'claude-opus-4-8', label: 'Claude Opus 4.8' }],
        } as never,
      });
      useSessionsStore.setState({ currentSession: { engine: 'inkwest-agent' } as never });
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      const msg: Message = { ...baseMessage, role: 'assistant', content: 'Answer.' };
      render(<MessageBubble message={msg} />);

      fireEvent.click(screen.getByRole('button', { name: /more copy options/i }));
      fireEvent.click(screen.getByRole('menuitem', { name: /copy as markdown/i }));

      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      const copied = writeText.mock.calls[0][0] as string;
      expect(copied).toContain('Inkwest Agent · Claude Opus 4.8');
      useSessionsStore.setState({ currentSession: null });
      vi.unstubAllGlobals();
    });

    it("uses the message's own engine over the session pin in the footer", async () => {
      useConfigStore.setState({
        config: {
          model: 'claude-opus-4-8',
          default_engine: 'claude-cli',
          available_models: [{ id: 'claude-opus-4-8', label: 'Claude Opus 4.8' }],
        } as never,
      });
      // Session is now pinned to the CLI, but this older turn ran on the agent — the
      // footer must reflect the turn, not the session's current pin.
      useSessionsStore.setState({ currentSession: { engine: 'claude-cli' } as never });
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      const msg: Message = {
        ...baseMessage,
        role: 'assistant',
        content: 'Answer.',
        engine: 'inkwest-agent',
      };
      render(<MessageBubble message={msg} />);

      fireEvent.click(screen.getByRole('button', { name: /more copy options/i }));
      fireEvent.click(screen.getByRole('menuitem', { name: /copy as markdown/i }));

      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      const copied = writeText.mock.calls[0][0] as string;
      expect(copied).toContain('Inkwest Agent');
      expect(copied).not.toContain('Claude Code');
      useSessionsStore.setState({ currentSession: null });
      vi.unstubAllGlobals();
    });

    it('primary "formatted" copy writes rich + plain via ClipboardItem', async () => {
      const write = vi.fn().mockResolvedValue(undefined);
      const seen: Record<string, unknown>[] = [];
      class FakeClipboardItem {
        constructor(items: Record<string, unknown>) {
          seen.push(items);
        }
      }
      vi.stubGlobal('ClipboardItem', FakeClipboardItem);
      vi.stubGlobal('navigator', { clipboard: { write } });
      const msg: Message = { ...baseMessage, role: 'assistant', content: '**bold** answer' };
      render(<MessageBubble message={msg} />);

      fireEvent.click(screen.getByRole('button', { name: /copy formatted/i }));

      await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
      expect(Object.keys(seen[0])).toEqual(['text/html', 'text/plain']);
      vi.unstubAllGlobals();
    });

    it('the menu offers a Teams-specific rich copy', async () => {
      const write = vi.fn().mockResolvedValue(undefined);
      const seen: Record<string, unknown>[] = [];
      class FakeClipboardItem {
        constructor(items: Record<string, unknown>) {
          seen.push(items);
        }
      }
      vi.stubGlobal('ClipboardItem', FakeClipboardItem);
      vi.stubGlobal('navigator', { clipboard: { write } });
      const msg: Message = { ...baseMessage, role: 'assistant', content: '```js\nx\n```' };
      render(<MessageBubble message={msg} />);

      fireEvent.click(screen.getByRole('button', { name: /more copy options/i }));
      fireEvent.click(screen.getByRole('menuitem', { name: /copy for teams/i }));

      await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
      expect(Object.keys(seen[0])).toEqual(['text/html', 'text/plain']);
      vi.unstubAllGlobals();
    });

    it('offers no copy button on user messages', () => {
      render(<MessageBubble message={baseMessage} />);
      expect(screen.queryByRole('button', { name: /copy formatted/i })).not.toBeInTheDocument();
    });

    it('offers no copy button on a tool-only assistant turn (no Markdown body)', () => {
      const msg: Message = {
        ...baseMessage,
        role: 'assistant',
        content: [{ type: 'tool_use', name: 'kubectl_get', input: {} }],
      };
      render(<MessageBubble message={msg} />);
      expect(screen.queryByRole('button', { name: /copy formatted/i })).not.toBeInTheDocument();
    });
  });

  describe('reuse prompt', () => {
    beforeEach(() => {
      useComposerStore.setState({ pending: null, nonce: 0 });
    });

    it('offers an Edit button on a non-empty string user message', () => {
      render(<MessageBubble message={baseMessage} />);
      expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
    });

    it('injects the raw prompt into the composer channel on click', () => {
      const msg: Message = { ...baseMessage, content: '  list pods\nin kube-system  ' };
      render(<MessageBubble message={msg} />);
      fireEvent.click(screen.getByRole('button', { name: /edit/i }));
      // Verbatim — internal newlines and surrounding whitespace preserved.
      expect(useComposerStore.getState().pending).toBe('  list pods\nin kube-system  ');
      expect(useComposerStore.getState().nonce).toBe(1);
    });

    it('offers no Edit button on assistant messages', () => {
      const msg: Message = { ...baseMessage, role: 'assistant', content: 'Here are your pods.' };
      render(<MessageBubble message={msg} />);
      expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
    });

    it('offers no Edit button on a whitespace-only user message', () => {
      const msg: Message = { ...baseMessage, content: '   ' };
      render(<MessageBubble message={msg} />);
      expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
    });

    it('offers no Edit button on a non-string (tool/block) user message', () => {
      const msg: Message = {
        ...baseMessage,
        content: [{ type: 'text', text: 'block content' }],
      };
      render(<MessageBubble message={msg} />);
      expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
    });
  });

  it('hides the timestamp on system events when showTimestamps is off (event text stays)', () => {
    useSettingsStore.setState({ showTimestamps: false });
    const msg: Message = {
      ...baseMessage,
      role: 'system',
      content: { type: 'session_event', subject: 'gitlab', action: 'granted' },
    };
    render(<MessageBubble message={msg} />);
    expect(screen.getByText(/GitLab access granted/)).toBeInTheDocument();
    expect(screen.queryByText(formatTimestamp(msg.created_at))).not.toBeInTheDocument();
  });
});
