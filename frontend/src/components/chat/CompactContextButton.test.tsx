import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CompactContextButton } from './CompactContextButton';
import { useChatStore } from '@/store/chat';
import { useConfigStore } from '@/store/config';
import { COMPACT_OFFER_PERCENT, COMPACT_OFFER_TOKENS } from '@/lib/contextUsage';
import type { Message } from '@/types/api';

const WINDOW = 200_000;
const MODEL = 'claude-sonnet-5';

function assistantWith(tokens: number, id = 'm2'): Message {
  return {
    id,
    session_id: 's1',
    role: 'assistant',
    content: 'done',
    input_tokens: tokens,
    created_at: new Date().toISOString(),
  } as Message;
}

const userTurn: Message = {
  id: 'm1',
  session_id: 's1',
  role: 'user',
  content: 'question',
  created_at: new Date().toISOString(),
} as Message;

function primeConfig(contextWindow: number | null) {
  useConfigStore.setState({
    config: {
      model: 'claude-sonnet-5',
      default_engine: 'claude-cli',
      engines: ['claude-cli'],
      attachments_enabled: false,
      available_models: [
        {
          id: 'claude-sonnet-5',
          label: 'Sonnet 5',
          legacy: false,
          engines: ['claude-cli'],
          input_modalities: [],
          context_window: contextWindow,
        },
      ],
      push_public_key: null,
    },
    fetchConfig: vi.fn(),
  });
}

function primeChat(messages: Message[], over: Partial<{ isStreaming: boolean; isConnected: boolean }> = {}) {
  useChatStore.setState({
    isStreaming: false,
    isConnected: true,
    messages,
    ...over,
  } as never);
}

/** A conversation under pressure: one question, one answer whose prompt was `percent` full. */
function pressured(percent: number): Message[] {
  return [userTurn, assistantWith(Math.round((percent / 100) * WINDOW))];
}

describe('CompactContextButton', () => {
  beforeEach(() => {
    primeConfig(WINDOW);
    primeChat([]);
  });

  it('stays hidden while the context is comfortable', () => {
    primeChat(pressured(COMPACT_OFFER_PERCENT - 20));
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('appears once the context reaches the offer threshold', () => {
    primeChat(pressured(COMPACT_OFFER_PERCENT));
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.getByRole('button', { name: /compact/i })).toBeInTheDocument();
  });

  it('states the cost so the user knows compaction spends a turn', () => {
    primeChat(pressured(95));
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.getByRole('button')).toHaveAttribute(
      'title',
      expect.stringContaining('Costs one turn')
    );
  });

  it('hides while a turn is running (the server would refuse a second one)', () => {
    primeChat(pressured(95), { isStreaming: true });
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is offered on an absolute prompt size when the window is unknown', () => {
    // Without this the escape hatch would simply not exist on an unsourced model.
    primeConfig(null);
    primeChat([userTurn, assistantWith(COMPACT_OFFER_TOKENS)]);
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('stays hidden on an unknown window while the prompt is still small', () => {
    primeConfig(null);
    primeChat([userTurn, assistantWith(1_000)]);
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('falls back to the global default model when the session has no pin', () => {
    primeChat(pressured(95));
    render(<CompactContextButton model={null} onCompact={vi.fn()} />);
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('goes quiet right after a compaction, when nothing is left to summarize', () => {
    // The summary turn's own prompt was the full pre-compaction context, so the gauge still
    // reads the peak — without the boundary check the button would offer a guaranteed no-op.
    primeChat([
      userTurn,
      assistantWith(190_000),
      {
        id: 'b1',
        session_id: 's1',
        role: 'system',
        content: { type: 'session_event', subject: 'context', action: 'compacted', targets: [] },
        created_at: new Date().toISOString(),
      } as Message,
    ]);
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is disabled while the socket is down', () => {
    primeChat(pressured(95), { isConnected: false });
    render(<CompactContextButton model={MODEL} onCompact={vi.fn()} />);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('asks the server to compact when clicked', async () => {
    primeChat(pressured(95));
    const onCompact = vi.fn();
    render(<CompactContextButton model={MODEL} onCompact={onCompact} />);
    await userEvent.click(screen.getByRole('button'));
    expect(onCompact).toHaveBeenCalledTimes(1);
  });
});
