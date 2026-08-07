import { Archive } from 'lucide-react';
import { useChatStore } from '@/store/chat';
import { useContextUsage, useShouldOfferCompaction } from '@/hooks/useContextUsage';
import { cn } from '@/lib/utils';

/**
 * Offers compaction once the conversation is under context pressure. Hidden below the
 * threshold so it doesn't invite a paid turn nobody needs, while a turn is running, and
 * once there is nothing left to summarize.
 */
export function CompactContextButton({
  model,
  onCompact,
  className,
}: {
  model: string | null;
  onCompact: () => void;
  className?: string;
}) {
  const isStreaming = useChatStore((s) => s.isStreaming);
  const isConnected = useChatStore((s) => s.isConnected);
  const offer = useShouldOfferCompaction(model);
  const usage = useContextUsage(model);

  if (isStreaming || !offer) return null;

  const fullness = usage ? `Context is ${Math.round(usage.percent)}% full. ` : '';
  return (
    <button
      type="button"
      onClick={onCompact}
      disabled={!isConnected}
      title={
        `${fullness}Compacting summarizes the conversation so far and continues from that ` +
        'summary — the full transcript stays visible here, and the assistant can still ' +
        'search it. Costs one turn.'
      }
      className={cn(
        'flex items-center gap-1 rounded-md border border-amber-500/40 px-2 py-1 text-xs',
        'text-amber-300 hover:bg-amber-500/10 transition-colors',
        'disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent',
        className
      )}
    >
      <Archive className="h-3 w-3" />
      Compact
    </button>
  );
}
