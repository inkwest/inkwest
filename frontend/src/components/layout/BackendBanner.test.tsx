import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { BackendBanner } from './BackendBanner';
import { useBackendStore } from '@/store/backend';

describe('BackendBanner', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false, serverErrors: {} });
  });

  it('renders nothing while the backend is reachable', () => {
    const { container } = render(<BackendBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('shows the reconnecting notice while the backend is unavailable', () => {
    useBackendStore.setState({ unavailable: true });
    render(<BackendBanner />);
    expect(screen.getByText('Backend unavailable — reconnecting…')).toBeInTheDocument();
  });

  it('shows a reload prompt once reconnection has given up', () => {
    useBackendStore.setState({ gaveUp: true });
    render(<BackendBanner />);
    expect(screen.getByText('Backend unreachable — reconnection gave up.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument();
  });

  it('give-up prompt takes precedence over the reconnecting notice', () => {
    useBackendStore.setState({ unavailable: true, gaveUp: true });
    render(<BackendBanner />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText('Backend unavailable — reconnecting…')).not.toBeInTheDocument();
  });

  it('surfaces reported 5xx errors with count, even while the backend is reachable', () => {
    useBackendStore.getState().reportServerError('GET /api/shares/pending', 500, 'column does not exist');
    useBackendStore.getState().reportServerError('GET /api/shares/pending', 500, 'column does not exist');
    render(<BackendBanner />);
    expect(screen.getByText('GET /api/shares/pending')).toBeInTheDocument();
    expect(screen.getByText(/×2/)).toBeInTheDocument();
    expect(screen.getByText(/column does not exist/)).toBeInTheDocument();
  });

  it('dismisses server errors and re-surfaces on a new failure', () => {
    useBackendStore.getState().reportServerError('POST /api/x', 500, 'boom');
    render(<BackendBanner />);
    fireEvent.click(screen.getByRole('button', { name: /dismiss backend errors/i }));
    expect(screen.queryByText('POST /api/x')).not.toBeInTheDocument();
    act(() => useBackendStore.getState().reportServerError('POST /api/x', 500, 'boom'));
    expect(screen.getByText('POST /api/x')).toBeInTheDocument();
  });

  it('shows the 5xx strip alongside the reconnecting notice', () => {
    useBackendStore.setState({ unavailable: true });
    useBackendStore.getState().reportServerError('GET /api/y', 500, 'oops');
    render(<BackendBanner />);
    expect(screen.getByText('Backend unavailable — reconnecting…')).toBeInTheDocument();
    expect(screen.getByText('GET /api/y')).toBeInTheDocument();
  });

  it('shows the 5xx strip alongside the give-up reload prompt too', () => {
    useBackendStore.setState({ gaveUp: true });
    useBackendStore.getState().reportServerError('GET /api/y', 500, 'oops');
    render(<BackendBanner />);
    expect(screen.getByText('Backend unreachable — reconnection gave up.')).toBeInTheDocument();
    expect(screen.getByText('GET /api/y')).toBeInTheDocument();
  });
});
