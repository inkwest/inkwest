import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConnectionStatus } from './ConnectionStatus';
import { useChatStore } from '@/store/chat';
import { useBackendStore } from '@/store/backend';

// The badge is icon-only: assert on its accessible name (role="img" + aria-label),
// which doubles as the title tooltip. No visible text labels any more.
describe('ConnectionStatus', () => {
  beforeEach(() => {
    useChatStore.setState({ isConnected: false, isReconnecting: false });
    useBackendStore.setState({ unavailable: false, gaveUp: false, retrying: 0 });
  });

  it('shows the connected dot when the socket is open', () => {
    useChatStore.setState({ isConnected: true, isReconnecting: false });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Connected' })).toBeInTheDocument();
  });

  it('shows the reconnecting dot while a retry is pending (backend otherwise up)', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: true });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Reconnecting…' })).toBeInTheDocument();
  });

  it('shows the disconnected dot once retries are exhausted', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: false });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Disconnected' })).toBeInTheDocument();
  });

  // Gate: defer to the global BackendBanner so the badge never echoes "reconnecting".
  it('defers to the global banner: neutral dot while backend unavailable, even mid-reconnect', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: true });
    useBackendStore.setState({ unavailable: true });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Connection status unavailable' })).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Reconnecting…' })).not.toBeInTheDocument();
  });

  it('defers to the global banner when it has given up (red reload), too', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: true });
    useBackendStore.setState({ gaveUp: true });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Connection status unavailable' })).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Reconnecting…' })).not.toBeInTheDocument();
  });

  it('still shows connected even while the global banner is up (chat stream is fine)', () => {
    useChatStore.setState({ isConnected: true, isReconnecting: false });
    useBackendStore.setState({ unavailable: true });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Connected' })).toBeInTheDocument();
  });

  it('shows connected even while the global banner has given up (an open stream wins)', () => {
    useChatStore.setState({ isConnected: true, isReconnecting: false });
    useBackendStore.setState({ gaveUp: true });
    render(<ConnectionStatus />);
    expect(screen.getByRole('img', { name: 'Connected' })).toBeInTheDocument();
  });
});
