import { CloudOff, RefreshCw } from 'lucide-react';
import { useBackendStore } from '@/store/backend';

// Non-blocking notice shown while the backend is unreachable — an AWS/ALB
// 502/503/504 or an outright network/timeout failure means the server is
// restarting (deploy/crash) or briefly gone. HTTP calls and WebSocket reconnects
// retry automatically in the background, so this only explains the momentarily
// frozen UI; it clears on the first response that reaches the app (or a WS
// reconnect). After the reconnect budget is exhausted (~10 min, backend looks
// permanently gone) it switches to a manual-reload prompt — recovery is otherwise
// automatic, no action button.
export function BackendBanner() {
  const unavailable = useBackendStore((s) => s.unavailable);
  const gaveUp = useBackendStore((s) => s.gaveUp);

  if (gaveUp) {
    return (
      <div
        role="alert"
        className="bg-red-900/60 border-b border-red-500 text-red-100 px-4 py-2 flex items-center gap-2"
      >
        <CloudOff className="h-4 w-4 shrink-0" />
        <span className="flex-1">Backend unreachable — reconnection gave up.</span>
        <button
          onClick={() => window.location.reload()}
          className="text-sm font-medium px-3 py-1 rounded-md bg-red-600 hover:bg-red-500 text-white inline-flex items-center gap-1"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Reload
        </button>
      </div>
    );
  }

  if (!unavailable) return null;

  return (
    <div
      role="status"
      className="bg-amber-900/60 border-b border-amber-500 text-amber-100 px-4 py-2 flex items-center gap-2"
    >
      <CloudOff className="h-4 w-4 shrink-0 animate-pulse" />
      <span className="flex-1">Backend unavailable — reconnecting…</span>
    </div>
  );
}
