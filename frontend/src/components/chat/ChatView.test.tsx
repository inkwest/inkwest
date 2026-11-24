import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ChatView } from './ChatView';
import { useChatStore } from '@/store/chat';
import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';
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
// Renders toolbarControls so the composer-toolbar wiring (the Compact button) is testable.
vi.mock('./ChatInput', () => ({
  ChatInput: ({ toolbarControls }: { toolbarControls?: React.ReactNode }) => (
    <div data-testid="chat-input">{toolbarControls}</div>
  ),
}));
vi.mock('./SessionModelSelect', () => ({ SessionModelSelect: () => null }));
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

describe('ChatView composer toolbar', () => {
  const pressured = [
    { id: 'u', session_id: 's1', role: 'user', content: 'q', created_at: '2026-01-01T00:00:00Z' },
    {
      id: 'a',
      session_id: 's1',
      role: 'assistant',
      content: 'a',
      context_tokens: 190_000,
      created_at: '2026-01-01T00:00:01Z',
    },
  ];

  beforeEach(() => {
    egress.state = { events: [], connected: false, disabled: false };
    useChatStore.setState({
      messages: pressured,
      streamingMessage: '',
      isStreaming: false,
      isConnected: true,
    } as never);
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
            context_window: 200_000,
          },
        ],
      },
      fetchConfig: vi.fn(),
    } as never);
    useSessionsStore.setState({
      currentSession: { id: 's1', model: 'claude-sonnet-5' },
    } as never);
  });

  it('mounts the Compact button in the toolbar when a compaction handler is supplied', () => {
    render(
      <MemoryRouter>
        <ChatView onSendMessage={vi.fn()} sessionId="s1" onCompact={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: /compact/i })).toBeInTheDocument();
  });

  it('omits it on a read-only conversation (no handler)', () => {
    render(
      <MemoryRouter>
        <ChatView onSendMessage={vi.fn()} sessionId="s1" />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('button', { name: /compact/i })).not.toBeInTheDocument();
  });
});
