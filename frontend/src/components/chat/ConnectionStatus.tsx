import { Wifi, WifiOff } from 'lucide-react';
import { useChatStore } from '@/store/chat';

export function ConnectionStatus() {
  const isConnected = useChatStore((s) => s.isConnected);

  return (
    <div className="flex items-center gap-2 text-sm">
      {isConnected ? (
        <>
          <Wifi className="h-4 w-4 text-green-400" />
          <span className="text-green-400">Connected</span>
        </>
      ) : (
        <>
          <WifiOff className="h-4 w-4 text-red-400" />
          <span className="text-red-400">Disconnected</span>
        </>
      )}
    </div>
  );
}
