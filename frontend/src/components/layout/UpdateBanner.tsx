import { RefreshCw, X } from 'lucide-react';
import { useUpdateStore } from '@/store/update';

// Non-blocking notice shown when a newer app version was deployed while this tab
// stayed open (detected on WebSocket reconnect). The user keeps working until
// they choose to reload; dismissing only hides it until the next reconnect.
export function UpdateBanner() {
  const updateAvailable = useUpdateStore((s) => s.updateAvailable);
  const availableVersion = useUpdateStore((s) => s.availableVersion);
  const dismiss = useUpdateStore((s) => s.dismiss);

  if (!updateAvailable) return null;

  return (
    <div
      role="status"
      className="bg-blue-900/60 border-b border-blue-500 text-blue-100 px-4 py-2 flex items-center gap-2"
    >
      <RefreshCw className="h-4 w-4 shrink-0" />
      <span className="flex-1">
        {`A new version of Inkwest is available${availableVersion ? ` (${availableVersion})` : ''}.`}
      </span>
      <button
        onClick={() => window.location.reload()}
        className="text-sm font-medium px-3 py-1 rounded-md bg-blue-600 hover:bg-blue-500 text-white"
      >
        Reload
      </button>
      <button onClick={dismiss} aria-label="Dismiss" className="text-blue-300 hover:text-blue-100">
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
