import { Modal } from '@/components/ui/Modal';
import { useInvitesStore } from '@/store/invites';

// Politely tells a user that a conversation shared with them has been un-shared
// (the owner revoked their access). Fed by a `share_revoked` event over the sync
// socket; shown one at a time. Mounted globally in the Layout.
export function ShareRevokedPopup() {
  const notices = useInvitesStore((s) => s.revokedNotices);
  const dismiss = useInvitesStore((s) => s.dismissRevokedNotice);

  const notice = notices[0];
  if (!notice) return null;

  return (
    <Modal
      isOpen
      onClose={() => dismiss(notice.sessionId)}
      title="Access removed"
      size="sm"
    >
      <p className="text-sm text-gray-300">
        {notice.title ? (
          <>
            <strong>{notice.title}</strong> is no longer shared with you.
          </>
        ) : (
          'This conversation is no longer shared with you.'
        )}
      </p>
      <div className="mt-5 flex justify-end">
        <button
          onClick={() => dismiss(notice.sessionId)}
          className="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white"
        >
          OK
        </button>
      </div>
    </Modal>
  );
}
