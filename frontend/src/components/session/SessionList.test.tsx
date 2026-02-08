import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SessionList } from './SessionList';
import { useSessionsStore } from '@/store/sessions';

vi.mock('./SessionItem', () => ({
  SessionItem: ({ session }: { session: { title?: string; id: string } }) => (
    <div data-testid={`session-${session.id}`}>{session.title || 'Untitled'}</div>
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

  it('shows error state', () => {
    useSessionsStore.setState({ error: 'Network error', isLoading: false });
    renderWithRouter();
    expect(screen.getByText('Failed to load sessions')).toBeInTheDocument();
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
});
