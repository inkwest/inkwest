import { RefreshCw } from 'lucide-react';

export function ReconnectingBanner() {
  return (
    <div className="bg-yellow-900/50 border border-yellow-500 text-yellow-200 px-4 py-2 flex items-center gap-2">
      <RefreshCw className="h-4 w-4 animate-spin" />
      <span>Reconnecting...</span>
    </div>
  );
}
