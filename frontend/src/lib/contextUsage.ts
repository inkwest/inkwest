import type { Message } from '@/types/api';

/** Display thresholds for the gauge colour only — whether to actually compact is decided
 *  server-side (spec 003, phase 3), so these never gate an action. */
export const GAUGE_WARN_PERCENT = 75;
export const GAUGE_ALERT_PERCENT = 90;

export interface ContextUsage {
  /** Prompt size of the last reported turn, in tokens. */
  used: number;
  /** The model's context window, in tokens. */
  window: number;
  /** `used / window` as 0-100, capped (a wire count can exceed a stale window value). */
  percent: number;
}

/**
 * How full the model's context is, derived from the last assistant turn that reported
 * usage: fresh `input_tokens` + `cache_read_tokens` + `cache_write_tokens` are all
 * input-side, so their sum is the prompt the engine actually sent.
 *
 * Returns null when the window is unknown or no turn has reported usage yet — the caller
 * renders nothing rather than a percentage of a guessed capacity. Assistant rows with no
 * usage (older rows, a turn that errored before its first API reply) are skipped, so a
 * failed retry doesn't blank a gauge that was live a moment ago.
 */
export function contextUsage(
  messages: Message[],
  contextWindow: number | null | undefined
): ContextUsage | null {
  if (!contextWindow || contextWindow <= 0) return null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role !== 'assistant') continue;
    const used =
      (msg.input_tokens ?? 0) + (msg.cache_read_tokens ?? 0) + (msg.cache_write_tokens ?? 0);
    if (used <= 0) continue;
    return {
      used,
      window: contextWindow,
      percent: Math.min(100, (used / contextWindow) * 100),
    };
  }
  return null;
}
