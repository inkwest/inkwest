import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BackendBanner } from './BackendBanner';
import { ConnectionStatus } from '@/components/chat/ConnectionStatus';
import { useChatStore } from '@/store/chat';
import { useBackendStore } from '@/store/backend';

// Invariant guarding the rationalization: when a backend restart drops everything at
// once (HTTP backing off AND the chat socket retrying), the user sees exactly ONE
// connectivity message — the global BackendBanner — and the chat header badge defers
// to it (neutral dot, no second "reconnecting" text).
describe('connectivity surface precedence', () => {
  beforeEach(() => {
    useChatStore.setState({ isConnected: false, isReconnecting: false });
    useBackendStore.setState({ unavailable: false, gaveUp: false, retrying: 0 });
  });

  it('shows a single reconnecting message when both HTTP backoff and chat retry are active', () => {
    useBackendStore.setState({ unavailable: true });
    useChatStore.setState({ isConnected: false, isReconnecting: true });

    render(
      <>
        <BackendBanner />
        <ConnectionStatus />
      </>
    );

    // Exactly one visible "reconnecting" message — the global banner. The badge is
    // icon-only (no visible text), so it contributes none.
    expect(screen.getAllByText(/reconnecting/i)).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent(/Backend unavailable — reconnecting/i);
    // Badge is in its gated, non-reconnecting state.
    expect(screen.getByRole('img', { name: 'Connection status unavailable' })).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Reconnecting…' })).not.toBeInTheDocument();
  });
});
