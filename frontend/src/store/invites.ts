import { create } from 'zustand';
import { PendingInvite } from '@/types/api';
import { getPendingInvites, respondToInvite } from '@/services/api';

// Pending conversation-share invitations addressed to the current user. Fed by a
// `share_invite` event over the sync socket (live) and by a fetch on load (recovery
// for invites that arrived while offline). Drives the accept/decline popup.
export interface RevokedNotice {
  sessionId: string;
  title: string | null;
}

interface InvitesState {
  invites: PendingInvite[];
  // Conversations whose share was just revoked for this user — drives a polite
  // "no longer shared with you" popup (delivered live over the sync socket).
  revokedNotices: RevokedNotice[];
  // Bumped when a participant's share status changes (accept/decline/revoke), so an
  // open ShareModal can reload its participant list live instead of on reopen.
  sharesVersion: number;
  fetchInvites: () => Promise<void>;
  addInvite: (invite: PendingInvite) => void;
  removeInvite: (shareId: string) => void;
  respond: (shareId: string, accept: boolean) => Promise<void>;
  addRevokedNotice: (notice: RevokedNotice) => void;
  dismissRevokedNotice: (sessionId: string) => void;
  bumpSharesVersion: () => void;
}

export const useInvitesStore = create<InvitesState>((set, get) => ({
  invites: [],
  revokedNotices: [],
  sharesVersion: 0,
  fetchInvites: async () => {
    try {
      set({ invites: await getPendingInvites() });
    } catch {
      // Best-effort: a transient failure just leaves the current list.
    }
  },
  addInvite: (invite) =>
    set((s) =>
      s.invites.some((i) => i.share_id === invite.share_id)
        ? s
        : { invites: [...s.invites, invite] }
    ),
  removeInvite: (shareId) =>
    set((s) => ({ invites: s.invites.filter((i) => i.share_id !== shareId) })),
  respond: async (shareId, accept) => {
    await respondToInvite(shareId, accept);
    get().removeInvite(shareId);
  },
  addRevokedNotice: (notice) =>
    set((s) =>
      s.revokedNotices.some((n) => n.sessionId === notice.sessionId)
        ? s
        : { revokedNotices: [...s.revokedNotices, notice] }
    ),
  dismissRevokedNotice: (sessionId) =>
    set((s) => ({ revokedNotices: s.revokedNotices.filter((n) => n.sessionId !== sessionId) })),
  bumpSharesVersion: () => set((s) => ({ sharesVersion: s.sharesVersion + 1 })),
}));
