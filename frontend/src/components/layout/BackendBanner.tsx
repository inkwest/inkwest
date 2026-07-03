import { CloudOff, RefreshCw, ServerCrash, X } from 'lucide-react';
import { useBackendStore } from '@/store/backend';

// Server-side failures (5xx from the app itself, not gateway blips): the backend is
// reachable but an endpoint is broken. Rendered as its own dismissible strip so a
// best-effort caller swallowing the error can't hide a real backend bug.
function ServerErrorStrip() {
  const serverErrors = useBackendStore((s) => s.serverErrors);
  const dismiss = useBackendStore((s) => s.dismissServerErrors);
  const entries = Object.entries(serverErrors);
  if (entries.length === 0) return null;

  return (
    <div
      role="alert"
      className="bg-red-900/60 border-b border-red-500 text-red-100 px-4 py-2 flex items-start gap-2 text-sm"
    >
      <ServerCrash className="h-4 w-4 shrink-0 mt-0.5" />
      {/* Bounded: the store caps entries, and the list scrolls past ~5 rows so a
          systemic outage can't shove the app below the fold. */}
      <div className="flex-1 min-w-0 space-y-0.5 max-h-32 overflow-y-auto">
        {entries.map(([key, e]) => (
          <div key={key} className="truncate" title={`${key} → ${e.status}: ${e.message}`}>
            <span className="font-mono">{key}</span> → {e.status}
            {e.count > 1 && <span className="text-red-300"> ×{e.count}</span>}
            <span className="text-red-200/80"> — {e.message}</span>
          </div>
        ))}
      </div>
      <button
        onClick={dismiss}
        aria-label="Dismiss backend errors"
        className="rounded p-1 hover:bg-red-800/60"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

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

  // Connectivity keeps its precedence (gaveUp > unavailable); the 5xx strip is an
  // independent, additive surface — the backend can be perfectly reachable while
  // one endpoint 500s.
  let connectivity = null;
  if (gaveUp) {
    connectivity = (
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
  } else if (unavailable) {
    connectivity = (
      <div
        role="status"
        className="bg-amber-900/60 border-b border-amber-500 text-amber-100 px-4 py-2 flex items-center gap-2"
      >
        <CloudOff className="h-4 w-4 shrink-0 animate-pulse" />
        <span className="flex-1">Backend unavailable — reconnecting…</span>
      </div>
    );
  }

  return (
    <>
      {connectivity}
      <ServerErrorStrip />
    </>
  );
}
