import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PresenceBar } from './PresenceBar';
import { useChatStore } from '@/store/chat';
import { useAuthStore } from '@/store/admin';

describe('PresenceBar', () => {
  beforeEach(() => {
    useChatStore.getState().resetChat();
    useAuthStore.setState({
      // Only `id` matters here (self-filtering); the rest satisfies the type.
      user: { id: 'me', email: '', display_name: 'Me', roles: [], oidc_issuer: '', is_admin: false },
    } as never);
  });

  it('renders nothing when you are the only viewer', () => {
    useChatStore.setState({ presence: [{ user_id: 'me', display_name: 'Me' }], typingUsers: [] });
    const { container } = render(<PresenceBar />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows other viewers (excluding self)', () => {
    useChatStore.setState({
      presence: [
        { user_id: 'me', display_name: 'Me' },
        { user_id: 'u2', display_name: 'Bob' },
      ],
      typingUsers: [],
    });
    render(<PresenceBar />);
    expect(screen.getByText(/1 person viewing/)).toBeInTheDocument();
  });

  it('shows a typing indicator for another user', () => {
    useChatStore.setState({
      presence: [],
      typingUsers: [{ user_id: 'u2', display_name: 'Bob' }],
    });
    render(<PresenceBar />);
    expect(screen.getByText(/Bob typing/)).toBeInTheDocument();
  });
});
