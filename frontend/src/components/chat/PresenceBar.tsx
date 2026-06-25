import { useChatStore } from '@/store/chat';
import { useAuthStore } from '@/store/admin';

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('');
}

// Thin "who's here" strip above the conversation: avatars of the other people
// currently viewing, plus a "X is typing…" line. Hidden when you're alone.
// Self is filtered out via the authenticated user id, or — on the anonymous shared
// view, which has no auth user — via `selfId` (the guest's own presence id from the
// server's presence_self frame).
export function PresenceBar({ selfId }: { selfId?: string | null } = {}) {
  const presence = useChatStore((s) => s.presence);
  const typingUsers = useChatStore((s) => s.typingUsers);
  const myAuthId = useAuthStore((s) => s.user?.id);
  const me = selfId ?? myAuthId;

  const others = presence.filter((p) => p.user_id !== me);
  const typing = typingUsers.filter((t) => t.user_id !== me);
  if (others.length === 0 && typing.length === 0) return null;

  return (
    <div className="flex items-center gap-2 px-4 py-1.5 border-b border-gray-800 bg-gray-900/30 text-xs text-gray-400 shrink-0">
      {others.length > 0 && (
        <>
          <span className="flex -space-x-1.5">
            {others.slice(0, 5).map((p) => (
              <span
                key={p.user_id}
                title={p.display_name}
                className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-blue-700 text-[10px] font-medium text-white ring-1 ring-gray-900"
              >
                {initials(p.display_name) || '?'}
              </span>
            ))}
          </span>
          <span>
            {others.length} {others.length === 1 ? 'person' : 'people'} viewing
          </span>
        </>
      )}
      {typing.length > 0 && (
        <span className="italic text-gray-500">
          {others.length > 0 ? '· ' : ''}
          {typing.map((t) => t.display_name || 'Someone').join(', ')} typing…
        </span>
      )}
    </div>
  );
}
