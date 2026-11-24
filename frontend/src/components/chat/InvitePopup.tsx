import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bell } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { usePushOptIn } from '@/hooks/usePushOptIn';
import { useInvitesStore } from '@/store/invites';
import { useOnboardingStore } from '@/store/onboarding';
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

  const tourDone = useOnboardingStore((s) => s.completed);

  const invite = invites.find((i) => !dismissed.has(i.share_id));
  // Wait for the onboarding tour: its spotlight would expose this modal over its targets.
  if (!invite || !tourDone) return null;

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
        <strong>{invite.invited_by_display_name || 'Someone'}</strong> shared a conversation
        with you (view-only).
      </p>
      {/* Title is user content in any language: kept out of the English sentence. */}
      {invite.session_title && (
        <p className="mt-2 text-sm text-gray-200 border-l-2 border-gray-600 pl-2 break-words">
          {invite.session_title}
        </p>
      )}
      <PushOffer />
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

// Opt-in at the moment it is relevant: the browser prompt only ever follows this click.
function PushOffer() {
  const { offer, permission, state, enable } = usePushOptIn();
  const done = state === 'on';
  const blocked = state === 'error' && permission === 'denied';
  // Mounted with the offer, filled later: screen readers announce changes, not insertions.
  if (!offer && !done && !blocked && state !== 'error') return null;

  return (
    <div className="mt-3">
      {!done && !blocked && (
        <button
          onClick={() => void enable()}
          disabled={state === 'busy'}
          className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"
        >
          <Bell className="h-3.5 w-3.5" />
          Notify me on this browser next time
        </button>
      )}
      <p role="status" className="text-xs text-gray-400">
        {done && 'Notifications are on for this browser.'}
        {blocked && 'Notifications are blocked for this site; allow them in the browser to change that.'}
        {state === 'dismissed' && 'No choice was made in the browser prompt.'}
      </p>
      {state === 'error' && !blocked && (
        <p role="alert" className="mt-1 text-xs text-red-400">
          Could not turn on notifications. Try again from Settings, Notifications.
        </p>
      )}
    </div>
  );
}
