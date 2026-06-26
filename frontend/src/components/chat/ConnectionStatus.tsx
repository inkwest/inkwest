import { Wifi, WifiOff, RefreshCw } from 'lucide-react';
import { useChatStore } from '@/store/chat';

export function ConnectionStatus() {
  const isConnected = useChatStore((s) => s.isConnected);
  const isReconnecting = useChatStore((s) => s.isReconnecting);

  if (isConnected) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Wifi className="h-4 w-4 text-green-400" />
        <span className="text-green-400">Connected</span>
      </div>
    );
  }

  // A retry is pending — distinguish a transient blip from a terminal disconnect.
  if (isReconnecting) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <RefreshCw className="h-4 w-4 text-yellow-400 animate-spin" />
        <span className="text-yellow-400">Reconnecting…</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 text-sm">
      <WifiOff className="h-4 w-4 text-red-400" />
      <span className="text-red-400">Disconnected</span>
    </div>
  );
}
