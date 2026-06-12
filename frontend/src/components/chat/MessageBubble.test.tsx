import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageBubble } from './MessageBubble';
import type { Message } from '@/types/api';
import { useSettingsStore } from '@/store/settings';
import { formatTimestamp } from '@/lib/utils';

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

  it('renders the timestamp in HH:MM format when showTimestamps is enabled', () => {
    useSettingsStore.setState({ showTimestamps: true });
    render(<MessageBubble message={baseMessage} />);
    const expected = formatTimestamp(baseMessage.created_at);
    expect(expected).toMatch(/^\d{2}:\d{2}/);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });
});
