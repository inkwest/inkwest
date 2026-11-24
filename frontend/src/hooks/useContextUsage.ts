import { useChatStore } from '@/store/chat';
import { useConfigStore } from '@/store/config';
import { contextUsage, shouldOfferCompaction, type ContextUsage } from '@/lib/contextUsage';
import { modelContextWindow } from '@/services/api';

/**
 * The context window of the model a conversation runs on: its own pin, else the global
 * default. Null when the model is unsourced — callers must render nothing rather than a
 * percentage of a guessed capacity.
 */
export function useContextWindow(model: string | null): number | null {
  const config = useConfigStore((s) => s.config);
  return modelContextWindow(config?.available_models ?? [], model ?? config?.model ?? null);
}

/**
 * Context occupancy for a conversation, or null when it can't be known (unsourced context
 * window, or no turn has reported usage since the last compaction).
 *
 * The model is passed in rather than read from the store: the header hides session context
 * inside the admin panel, and the caller owns that decision.
 */
export function useContextUsage(model: string | null): ContextUsage | null {
  const messages = useChatStore((s) => s.messages);
  const contextWindow = useContextWindow(model);
  return contextUsage(messages, contextWindow);
}

/** Whether to offer compaction: real pressure AND something left to compact. */
export function useShouldOfferCompaction(model: string | null): boolean {
  const messages = useChatStore((s) => s.messages);
  const contextWindow = useContextWindow(model);
  return shouldOfferCompaction(messages, contextWindow);
}
