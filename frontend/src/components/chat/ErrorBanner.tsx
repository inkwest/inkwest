import { AlertCircle, X } from 'lucide-react';
import { useChatStore } from '@/store/chat';

export function ErrorBanner() {
  const { error, setError } = useChatStore();

  if (!error) return null;

  return (
    <div className="bg-red-900/50 border border-red-500 text-red-200 px-4 py-2 flex items-center gap-2">
      <AlertCircle className="h-4 w-4" />
      <span className="flex-1">{error}</span>
      <button onClick={() => setError(null)}>
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
