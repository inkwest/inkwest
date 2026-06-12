import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SessionList } from './SessionList';
import { useSessionsStore } from '@/store/sessions';

// Spread dragHandleProps onto the stub so the drag tests can fire dragstart/
// dragend on it; dragover/drop land on SessionList's wrapper div via bubbling.
vi.mock('./SessionItem', () => ({
  SessionItem: ({
    session,
    dragHandleProps,
  }: {
    session: { title?: string; id: string };
    dragHandleProps?: object;
  }) => (
    <div data-testid={`session-${session.id}`} {...dragHandleProps}>
      {session.title || 'Untitled'}
    </div>
  ),
}));

function renderWithRouter() {
  return render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
}

const noop = vi.fn();

describe('SessionList', () => {
  beforeEach(() => {
    useSessionsStore.setState({
      sessions: [],
      isLoading: false,
      error: null,
      searchResults: null,
      isSearching: false,
      fetchSessions: noop,
      searchGlobal: noop,
      clearSearch: noop,
    });
  });

  it('shows loading state', () => {
    useSessionsStore.setState({ isLoading: true });
    renderWithRouter();
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('shows a recoverable error state with a retry that refetches', () => {
    const retry = vi.fn();
    useSessionsStore.setState({ error: 'Network error', isLoading: false, fetchSessions: retry });
    renderWithRouter();
    expect(screen.getByText(/couldn't load your sessions/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(retry).toHaveBeenCalled();
  });

  it('shows empty state', () => {
    useSessionsStore.setState({ sessions: [], isLoading: false });
    renderWithRouter();
    expect(screen.getByText('No sessions yet')).toBeInTheDocument();
  });

  it('renders session list', () => {
    useSessionsStore.setState({
      isLoading: false,
      sessions: [
        {
          id: 's1',
          cluster_id: 'c1',
          title: 'Debug pod crash',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        {
          id: 's2',
          cluster_id: 'c1',
          title: 'Check node status',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ],
    });
    renderWithRouter();
    expect(screen.getByTestId('session-s1')).toHaveTextContent('Debug pod crash');
    expect(screen.getByTestId('session-s2')).toHaveTextContent('Check node status');
  });

  it('renders search input', () => {
    useSessionsStore.setState({ isLoading: false, sessions: [] });
    renderWithRouter();
    expect(screen.getByPlaceholderText(/search all sessions/i)).toBeInTheDocument();
  });

  it('deep-links a matched message result to the conversation + message anchor', () => {
    useSessionsStore.setState({
      isLoading: false,
      searchResults: {
        query: 'po',
        total: 1,
        results: [
          {
            id: 'sess-1',
            title: 'Debug',
            cluster: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            title_match: false,
            matched_messages: [
              { id: 'msg-9', role: 'user', snippet: 'pod crashed', created_at: new Date().toISOString() },
            ],
          },
        ],
      },
    });
    const { container } = renderWithRouter();
    // Two chars enters search mode (isInSearchMode); the stored results then render.
    fireEvent.change(screen.getByPlaceholderText(/search all sessions/i), { target: { value: 'po' } });
    // The message hit links to the conversation with the message anchor (?m=<id>),
    // distinct from the session-title link which omits it.
    expect(container.querySelector('a[href="/session/sess-1?m=msg-9"]')).toBeInTheDocument();
    expect(container.querySelector('a[href="/session/sess-1"]')).toBeInTheDocument();
  });

  describe('drag reorder', () => {
    const session = (id: string) => ({
      id,
      cluster_id: 'c1',
      title: id,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    const moveSession = vi.fn();
    const persistSessionOrder = vi.fn();
    // jsdom has no DataTransfer; fireEvent assigns this stub onto the event.
    const dataTransfer = () => ({ effectAllowed: '', dropEffect: '', setData: vi.fn() });

    beforeEach(() => {
      moveSession.mockClear();
      persistSessionOrder.mockClear();
      useSessionsStore.setState({
        sessions: [session('s1'), session('s2'), session('s3')],
        moveSession,
        persistSessionOrder,
      });
    });

    const startDrag = (id: string) => {
      const dt = dataTransfer();
      fireEvent.dragStart(screen.getByTestId(`session-${id}`), { dataTransfer: dt });
      return dt;
    };

    it('dragstart marks the drag as a move and sets the session id as payload', () => {
      renderWithRouter();
      const dt = startDrag('s1');
      expect(dt.effectAllowed).toBe('move');
      expect(dt.setData).toHaveBeenCalledWith('text/plain', 's1');
    });

    it('dragover another row accepts the drop and live-moves the session', () => {
      renderWithRouter();
      startDrag('s1');
      // fireEvent returns false when preventDefault was called (drop accepted)
      const accepted = !fireEvent.dragOver(screen.getByTestId('session-s3'), {
        dataTransfer: dataTransfer(),
      });
      expect(accepted).toBe(true);
      expect(moveSession).toHaveBeenCalledWith('s1', 's3');
    });

    it('dragover the dragged row itself stays accepted but does not move', () => {
      renderWithRouter();
      startDrag('s2');
      // Steady state after a live move — Firefox needs the target to keep
      // accepting or it falls back to default unhandled-drop handling.
      const accepted = !fireEvent.dragOver(screen.getByTestId('session-s2'), {
        dataTransfer: dataTransfer(),
      });
      expect(accepted).toBe(true);
      expect(moveSession).not.toHaveBeenCalled();
    });

    it('dragover without an in-app drag (foreign drag) is not accepted', () => {
      renderWithRouter();
      const accepted = !fireEvent.dragOver(screen.getByTestId('session-s2'), {
        dataTransfer: dataTransfer(),
      });
      expect(accepted).toBe(false);
      expect(moveSession).not.toHaveBeenCalled();
    });

    it('dragend persists the order exactly once', () => {
      renderWithRouter();
      startDrag('s1');
      fireEvent.dragOver(screen.getByTestId('session-s3'), { dataTransfer: dataTransfer() });
      fireEvent.dragEnd(screen.getByTestId('session-s1'), { dataTransfer: dataTransfer() });
      expect(persistSessionOrder).toHaveBeenCalledTimes(1);
    });

    it('does not persist on a grab-and-release with no actual move', () => {
      renderWithRouter();
      startDrag('s1');
      // No dragover onto another row → nothing reordered.
      fireEvent.dragEnd(screen.getByTestId('session-s1'), { dataTransfer: dataTransfer() });
      expect(persistSessionOrder).not.toHaveBeenCalled();
    });

    it('forgets the drag when the dragged session disappears mid-drag', () => {
      renderWithRouter();
      startDrag('s1');
      // Remote delete arriving via the realtime sync — s1's dragend never fires.
      act(() => {
        useSessionsStore.setState({ sessions: [session('s2'), session('s3')] });
      });
      const accepted = !fireEvent.dragOver(screen.getByTestId('session-s2'), {
        dataTransfer: dataTransfer(),
      });
      expect(accepted).toBe(false);
      expect(moveSession).not.toHaveBeenCalled();
    });
  });
});
