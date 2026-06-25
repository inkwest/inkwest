import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Modal } from '@/components/ui/Modal';
import { useInvitesStore } from '@/store/invites';
import { useSessionsStore } from '@/store/sessions';

// Surfaces pending conversation-share invitations one at a time. Accept joins the
// conversation (read-only) and navigates to it; Decline records the refusal; closing
// just dismisses for this session (the invite stays pending, recoverable on reload).
export function InvitePopup() {
  const invites = useInvitesStore((s) => s.invites);
  const respond = useInvitesStore((s) => s.respond);
  const fetchSessions = useSessionsStore((s) => s.fetchSessions);
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const invite = invites.find((i) => !dismissed.has(i.share_id));
  if (!invite) return null;

  const act = async (accept: boolean) => {
    setBusy(true);
    try {
      await respond(invite.share_id, accept);
      if (accept) {
        await fetchSessions();
        navigate(`/session/${invite.session_id}`);
      }
    } catch {
      // Leave the invite in place on failure so the user can retry.
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      isOpen
      onClose={() => setDismissed((d) => new Set(d).add(invite.share_id))}
      title="Conversation shared with you"
      size="sm"
    >
      <p className="text-sm text-gray-300">
        <strong>{invite.invited_by_display_name || 'Someone'}</strong> shared{' '}
        <strong>{invite.session_title || 'a conversation'}</strong> with you (view-only).
      </p>
      <div className="mt-5 flex justify-end gap-2">
        <button
          onClick={() => void act(false)}
          disabled={busy}
          className="px-3 py-1.5 text-sm rounded bg-gray-700 hover:bg-gray-600 text-gray-200 disabled:opacity-50"
        >
          Decline
        </button>
        <button
          onClick={() => void act(true)}
          disabled={busy}
          className="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
        >
          Accept
        </button>
      </div>
    </Modal>
  );
}
