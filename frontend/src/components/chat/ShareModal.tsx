import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, Link2, Trash2, UserPlus } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import {
  createShareLink,
  getShareLinks,
  getSessionShares,
  getShareableUsers,
  revokeShare,
  revokeShareLink,
  shareSession,
} from '@/services/api';
import { Participant, ShareLink, UserSummary } from '@/types/api';
import { useInvitesStore } from '@/store/invites';
import { useSessionsStore } from '@/store/sessions';

interface Props {
  sessionId: string;
  isOpen: boolean;
  onClose: () => void;
}

// Read-only conversation sharing: invite system users (they get an accept/decline
// popup) and/or mint revokable anonymous links. Everyone shared with is view-only;
// only the owner sends.
export function ShareModal({ sessionId, isOpen, onClose }: Props) {
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [participants, setParticipants] = useState<Participant[]>([]);
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  // Bumped by the sync socket when a participant accepts/declines/is revoked, so an
  // open modal reflects the new status live (not only on reopen).
  const sharesVersion = useInvitesStore((s) => s.sharesVersion);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [u, p, l] = await Promise.all([
        getShareableUsers(),
        getSessionShares(sessionId),
        getShareLinks(sessionId),
      ]);
      setUsers(u);
      setParticipants(p);
      setLinks(l);
      // Keep the tab-row "Shared·N" badge in sync with what we just loaded (people
      // invited-or-accepted + active links) without a full session refetch.
      const count = p.filter((x) => x.status !== 'declined').length + l.length;
      useSessionsStore.getState().setShareCount(sessionId, count);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load sharing');
    }
  }, [sessionId]);

  useEffect(() => {
    if (isOpen) void load();
  }, [isOpen, load, sharesVersion]);

  // Users not already invited/participating, filtered by the search box.
  const sharedIds = new Set(participants.map((p) => p.user_id));
  const candidates = users
    .filter((u) => !sharedIds.has(u.id))
    .filter(
      (u) =>
        !filter ||
        u.display_name.toLowerCase().includes(filter.toLowerCase()) ||
        u.email.toLowerCase().includes(filter.toLowerCase())
    );

  const invite = async (userId: string) => {
    setBusy(true);
    setError(null);
    try {
      await shareSession(sessionId, userId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to share');
    } finally {
      setBusy(false);
    }
  };

  const unshare = async (userId: string) => {
    setBusy(true);
    try {
      await revokeShare(sessionId, userId);
      await load();
    } finally {
      setBusy(false);
    }
  };

  const mintLink = async () => {
    setBusy(true);
    setError(null);
    try {
      await createShareLink(sessionId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create link');
    } finally {
      setBusy(false);
    }
  };

  const dropLink = async (linkId: string) => {
    setBusy(true);
    try {
      await revokeShareLink(sessionId, linkId);
      await load();
    } finally {
      setBusy(false);
    }
  };

  const copyLink = async (link: ShareLink) => {
    const full = `${window.location.origin}${link.url}`;
    try {
      await navigator.clipboard.writeText(full);
      setCopied(link.id);
      setTimeout(() => setCopied((c) => (c === link.id ? null : c)), 1500);
    } catch {
      // Clipboard blocked — leave the URL visible for manual copy.
    }
  };

  const statusBadge = (p: Participant) => {
    const tone =
      p.status === 'accepted'
        ? 'bg-green-900/40 text-green-400'
        : p.status === 'pending'
          ? 'bg-yellow-900/40 text-yellow-400'
          : 'bg-gray-700 text-gray-400';
    return <span className={`text-[10px] px-1.5 py-0.5 rounded ${tone}`}>{p.status}</span>;
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Share conversation" size="md">
      <p className="text-xs text-gray-400 mb-4">
        Shared people and links are <strong>read-only</strong> — they can follow the conversation
        live but only you can send messages.
      </p>

      {error && (
        <div className="mb-3 text-xs text-red-400 bg-red-900/30 rounded px-3 py-2">{error}</div>
      )}

      {/* People */}
      <section className="mb-5">
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2 flex items-center gap-1.5">
          <UserPlus className="h-3.5 w-3.5" /> People
        </h4>

        {participants.length > 0 && (
          <ul className="mb-2 space-y-1">
            {participants.map((p) => (
              <li
                key={p.id}
                className="flex items-center justify-between gap-2 text-sm bg-gray-900/40 rounded px-2 py-1.5"
              >
                <span className="min-w-0">
                  <span className="text-gray-200 truncate">{p.display_name}</span>{' '}
                  {statusBadge(p)}
                </span>
                <button
                  onClick={() => void unshare(p.user_id)}
                  disabled={busy}
                  className="text-gray-500 hover:text-red-400 disabled:opacity-50"
                  title="Remove access"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Search people to invite…"
          className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500"
        />
        {filter && (
          <ul className="mt-1 max-h-40 overflow-y-auto space-y-1">
            {candidates.length === 0 && (
              <li className="text-xs text-gray-500 px-2 py-1">No matching users</li>
            )}
            {candidates.map((u) => (
              <li key={u.id}>
                <button
                  onClick={() => void invite(u.id)}
                  disabled={busy}
                  className="w-full flex items-center justify-between gap-2 text-sm text-left px-2 py-1.5 rounded hover:bg-gray-700 disabled:opacity-50"
                >
                  <span className="min-w-0 truncate">
                    <span className="text-gray-200">{u.display_name}</span>
                    <span className="text-gray-500 ml-1.5 text-xs">{u.email}</span>
                  </span>
                  <UserPlus className="h-4 w-4 text-blue-400 shrink-0" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Links */}
      <section>
        <div className="flex items-center justify-between mb-2">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 flex items-center gap-1.5">
            <Link2 className="h-3.5 w-3.5" /> Share links
          </h4>
          <button
            onClick={() => void mintLink()}
            disabled={busy}
            className="text-xs px-2 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
          >
            Create link
          </button>
        </div>
        <p className="text-[11px] text-gray-500 mb-2">
          Anyone with a link can view this conversation without signing in. Revoke any time.
        </p>
        {links.length === 0 ? (
          <p className="text-xs text-gray-600">No active links.</p>
        ) : (
          <ul className="space-y-1">
            {links.map((link) => (
              <li
                key={link.id}
                className="flex items-center gap-2 bg-gray-900/40 rounded px-2 py-1.5"
              >
                <code className="flex-1 min-w-0 truncate text-xs text-gray-400">
                  {window.location.origin}
                  {link.url}
                </code>
                <button
                  onClick={() => void copyLink(link)}
                  className="text-gray-400 hover:text-white shrink-0"
                  title="Copy link"
                >
                  {copied === link.id ? (
                    <Check className="h-4 w-4 text-green-400" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                </button>
                <button
                  onClick={() => void dropLink(link.id)}
                  disabled={busy}
                  className="text-gray-500 hover:text-red-400 shrink-0 disabled:opacity-50"
                  title="Revoke link"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </Modal>
  );
}
