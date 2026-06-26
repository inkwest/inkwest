import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ChatView } from './ChatView';
import { useChatStore } from '@/store/chat';
import type { EgressEvent } from '@/hooks/useEgressSocket';

// Drive the egress socket state per test. The mock reads `egress.state` fresh on every
// render, so tests reassign it before rendering.
const egress = vi.hoisted(() => ({
  state: { events: [] as EgressEvent[], connected: false, disabled: false },
}));
vi.mock('@/hooks/useEgressSocket', () => {
  // Mirror the real predicates: only a DENIED decision is a squid block; a 4xx/5xx
  // on a forwarded request is an upstream error, not a block.
  const isBlocked = (e: EgressEvent) => e.decision.includes('DENIED');
  return {
    useEgressSocket: () => egress.state,
    isBlocked,
    isError: (e: EgressEvent) =>
      !isBlocked(e) && (e.status.startsWith('4') || e.status.startsWith('5')),
  };
});

// Stub heavy children so the test isolates ChatView's own markup (the tab bar).
vi.mock('./MessageList', () => ({ MessageList: () => <div data-testid="message-list" /> }));
vi.mock('./ChatInput', () => ({ ChatInput: () => <div data-testid="chat-input" /> }));
vi.mock('./StreamingMessage', () => ({ StreamingMessage: () => null }));
vi.mock('./ErrorBanner', () => ({ ErrorBanner: () => null }));
vi.mock('./EgressPanel', () => ({ EgressLog: () => <div data-testid="egress-log" /> }));
vi.mock('@/components/connector/ConnectorPanel', () => ({ ConnectorPanel: () => null }));
vi.mock('./ConnectorInlineNotice', () => ({ ConnectorInlineNotice: () => null }));

function event(partial: Partial<EgressEvent>): EgressEvent {
  return {
    _id: 0,
    ts: 0,
    decision: 'TCP_TUNNEL',
    status: '200',
    method: 'CONNECT',
    host: 'example.com',
    port: '443',
    bytes: 0,
    client: 'c',
    ...partial,
  };
}

function renderView() {
  return render(
    <MemoryRouter>
      <ChatView onSendMessage={vi.fn()} sessionId="s1" />
    </MemoryRouter>,
  );
}

describe('ChatView tab bar', () => {
  beforeEach(() => {
    egress.state = { events: [], connected: false, disabled: false };
    useChatStore.setState({
      messages: [],
      streamingMessage: '',
      isStreaming: false,
      isConnected: true,
      streamingBlocks: [],
    });
  });

  it('always renders the tab bar with a disabled Network tab before egress connects', () => {
    renderView();
    expect(screen.getByRole('button', { name: 'Conversation' })).toBeEnabled();
    expect(screen.getByRole('button', { name: /network/i })).toBeDisabled();
  });

  it('enables the Network tab and shows event + blocked + error counts once egress is available', () => {
    egress.state = {
      events: [
        event({ _id: 1, decision: 'TCP_TUNNEL', status: '200' }),
        event({ _id: 2, decision: 'TCP_DENIED', status: '403' }),
        // Upstream 503 on a forwarded tunnel — an error, NOT a squid block.
        event({ _id: 3, decision: 'TCP_TUNNEL', status: '503' }),
      ],
      connected: true,
      disabled: false,
    };
    renderView();

    const network = screen.getByRole('button', { name: /network/i });
    expect(network).toBeEnabled();
    expect(network).toHaveTextContent('3'); // event count badge
    expect(network).toHaveTextContent('1 blocked'); // only the TCP_DENIED/403
    expect(network).toHaveTextContent('1 errors'); // the TCP_TUNNEL/503
  });

  it('enables the Network tab on connect even before any events arrive', () => {
    egress.state = { events: [], connected: true, disabled: false };
    renderView();
    expect(screen.getByRole('button', { name: /network/i })).toBeEnabled();
  });
});
