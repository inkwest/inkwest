import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageList } from './MessageList';
import type { Message } from '@/types/api';

const failed: Message = {
  id: 'msg-fail',
  session_id: 'sess-1',
  role: 'assistant',
  content: 'partial answer',
  status: 'error',
  error_message: 'boom',
  is_retryable: true,
  created_at: '2026-06-11T14:30:00Z',
};

const systemEvent: Message = {
  id: 'msg-sys',
  session_id: 'sess-1',
  role: 'system',
  content: { type: 'session_event', subject: 'model', action: 'changed', targets: [{ model: 'claude-opus-4-8' }] },
  created_at: '2026-06-11T14:31:00Z',
};

describe('MessageList — latest-turn resolution for retry', () => {
  it('keeps retry on a failed turn when only trailing system events follow it', () => {
    render(<MessageList messages={[failed, systemEvent]} onRetry={() => {}} />);
    // The system event is not a chat turn, so the failed assistant turn is still "latest".
    expect(screen.getByText(/retry/i)).toBeInTheDocument();
  });

  it('drops retry on a failed turn once a newer chat turn follows it', () => {
    const newer: Message = { ...failed, id: 'msg-2', status: undefined, is_retryable: undefined, content: 'next' };
    render(<MessageList messages={[failed, newer]} onRetry={() => {}} />);
    expect(screen.queryByText(/retry/i)).not.toBeInTheDocument();
  });
});
