import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useInvitesStore } from './invites';

vi.mock('@/services/api', () => ({
  getPendingInvites: vi.fn(async () => []),
  respondToInvite: vi.fn(async () => ({ status: 'accepted', session_id: 's1' })),
}));

const invite = (id: string) => ({
  share_id: id,
  session_id: 's1',
  session_title: 'Prod incident',
  role: 'viewer',
  invited_by_display_name: 'Alice',
  created_at: '',
});

describe('invites store', () => {
  beforeEach(() => useInvitesStore.setState({ invites: [], revokedNotices: [], sharesVersion: 0 }));

  it('dedupes addInvite by share_id', () => {
    useInvitesStore.getState().addInvite(invite('x'));
    useInvitesStore.getState().addInvite(invite('x'));
    expect(useInvitesStore.getState().invites).toHaveLength(1);
  });

  it('removeInvite drops the matching invite', () => {
    useInvitesStore.getState().addInvite(invite('a'));
    useInvitesStore.getState().addInvite(invite('b'));
    useInvitesStore.getState().removeInvite('a');
    expect(useInvitesStore.getState().invites.map((i) => i.share_id)).toEqual(['b']);
  });

  it('respond removes the invite after the API call', async () => {
    useInvitesStore.getState().addInvite(invite('y'));
    await useInvitesStore.getState().respond('y', true);
    expect(useInvitesStore.getState().invites).toHaveLength(0);
  });

  it('addRevokedNotice dedupes by sessionId and dismiss clears it', () => {
    useInvitesStore.getState().addRevokedNotice({ sessionId: 's1', title: 'Prod incident' });
    useInvitesStore.getState().addRevokedNotice({ sessionId: 's1', title: 'Prod incident' });
    expect(useInvitesStore.getState().revokedNotices).toHaveLength(1);
    useInvitesStore.getState().dismissRevokedNotice('s1');
    expect(useInvitesStore.getState().revokedNotices).toHaveLength(0);
  });

  it('bumpSharesVersion increments the reload signal', () => {
    useInvitesStore.getState().bumpSharesVersion();
    expect(useInvitesStore.getState().sharesVersion).toBe(1);
  });
});
