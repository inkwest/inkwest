import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BackendBanner } from './BackendBanner';
import { useBackendStore } from '@/store/backend';

describe('BackendBanner', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false });
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
});
