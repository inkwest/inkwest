import { useEffect } from 'react';
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { ShellLayout } from './ShellLayout';
import { InvitePopup } from '@/components/chat/InvitePopup';
import { ShareRevokedPopup } from '@/components/chat/ShareRevokedPopup';
import { useSyncSocket } from '@/hooks/useSyncSocket';
import { useInvitesStore } from '@/store/invites';
import { useConfigStore } from '@/store/config';
import { syncPushSubscription } from '@/services/push';

export function Layout() {
  useSyncSocket();
  const fetchInvites = useInvitesStore((s) => s.fetchInvites);

  // Recover invitations that arrived while this user was offline (live ones come
  // over the sync socket); the popup then surfaces them.
  useEffect(() => {
    void fetchInvites();
  }, [fetchInvites]);

  // Keyed on the key itself, so a config refresh with the same key doesn't re-sync.
  const pushKey = useConfigStore((s) => s.config?.push_public_key);
  useEffect(() => {
    if (pushKey) syncPushSubscription(pushKey).catch(() => {});
  }, [pushKey]);

  return (
    <ShellLayout sidebar={<Sidebar />}>
      <Outlet />
      <InvitePopup />
      <ShareRevokedPopup />
    </ShellLayout>
  );
}
