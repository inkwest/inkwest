import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConnectionStatus } from './ConnectionStatus';
import { useChatStore } from '@/store/chat';

describe('ConnectionStatus', () => {
  beforeEach(() => {
    useChatStore.setState({ isConnected: false, isReconnecting: false });
  });

  it('shows "Connected" when the socket is open', () => {
    useChatStore.setState({ isConnected: true, isReconnecting: false });
    render(<ConnectionStatus />);
    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('shows "Reconnecting…" while a retry is pending (takes precedence over disconnected)', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: true });
    render(<ConnectionStatus />);
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(screen.queryByText('Disconnected')).not.toBeInTheDocument();
  });

  it('shows "Disconnected" only once retries are exhausted', () => {
    useChatStore.setState({ isConnected: false, isReconnecting: false });
    render(<ConnectionStatus />);
    expect(screen.getByText('Disconnected')).toBeInTheDocument();
  });
});
