import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MessageBubble } from './MessageBubble';
import type { Message } from '@/types/api';
import { useSettingsStore } from '@/store/settings';
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
