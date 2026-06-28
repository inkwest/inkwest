import { Wifi, WifiOff, RefreshCw, type LucideIcon } from 'lucide-react';
import { useChatStore } from '@/store/chat';
import { useBackendStore } from '@/store/backend';
import { cn } from '@/lib/utils';

// Header connectivity dot — a chat-socket-specific signal, kept deliberately quiet so
// it never duplicates the global BackendBanner. Icon-only: the label lives in
// title/aria-label, not visible text, so it adds no second "reconnecting" message.
//
// Precedence (BackendBanner is the single global connectivity surface): while the
// global banner is up (HTTP backoff or a sync-socket give-up), the chat socket is down
// *because* the backend is — the banner already says so. So we suppress the alarming
// reconnecting/disconnected states here and show a neutral dot, surfacing the chat
// state on its own only in the case the global banner misses: backend up, stream blipped.
function StatusDot({ icon: Icon, className, label, spin }: {
  icon: LucideIcon;
  className: string;
  label: string;
  spin?: boolean;
}) {
  // role="img" + a non-live aria-label: announced on focus, never as a live region —
  // the global banner owns aria-live, so the dot must not double-announce.
  return (
    <span role="img" aria-label={label} title={label} className="flex items-center">
      <Icon className={cn('h-4 w-4', className, spin && 'animate-spin')} aria-hidden="true" />
    </span>
  );
}

export function ConnectionStatus() {
  const isConnected = useChatStore((s) => s.isConnected);
  const isReconnecting = useChatStore((s) => s.isReconnecting);
  const backendDown = useBackendStore((s) => s.unavailable || s.gaveUp);

  if (isConnected) {
    return <StatusDot icon={Wifi} className="text-green-400" label="Connected" />;
  }

  // Defer to the global banner: don't echo "reconnecting"/"disconnected" while it speaks.
  if (backendDown) {
    return <StatusDot icon={Wifi} className="text-gray-500" label="Connection status unavailable" />;
  }

  // Backend is up but the chat stream dropped — the one case the global banner misses.
  if (isReconnecting) {
    return <StatusDot icon={RefreshCw} className="text-yellow-400" label="Reconnecting…" spin />;
  }

  return <StatusDot icon={WifiOff} className="text-red-400" label="Disconnected" />;
}
