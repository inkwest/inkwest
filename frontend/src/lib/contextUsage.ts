import type { Message } from '@/types/api';

/** Gauge colour thresholds (percent of the model's context window). */
export const GAUGE_WARN_PERCENT = 75;
export const GAUGE_ALERT_PERCENT = 90;

/** Percent of a known window past which compaction is offered. */
export const COMPACT_OFFER_PERCENT = 75;
/**
 * Absolute prompt size past which compaction is offered when the model's window is
 * unknown. Without this the escape hatch would simply not exist on an unsourced model —
 * pressure is still measurable in tokens, only the ratio isn't.
 */
export const COMPACT_OFFER_TOKENS = 150_000;

export interface ContextUsage {
  /** Prompt size of the last reported turn, in tokens. */
  used: number;
  /** The model's context window, in tokens. */
  window: number;
  /** `used / window` as 0-100, capped (a wire count can exceed a stale window value). */
  percent: number;
}

/**
 * Prompt size of the last assistant turn that reported it: `context_tokens`, the prompt of
 * the turn's LAST model call. Not the row's input + cache counts: those sum every tool
 * iteration of the turn and overstate occupancy several times over. Null before any turn
 * has reported it.
 *
 * Assistant rows without it (rows predating the field, an older agent image, a turn that
 * errored before its first API reply) are skipped, so a failed retry doesn't blank a
 * reading that was live a moment ago.
 */
export function lastPromptTokens(messages: Message[]): number | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    // Stop at a compaction boundary: every reading before it measured the context that was
    // just reclaimed. Reporting it would leave the gauge red right after a paid compaction,
    // and re-offer the button at a stale 96% the moment any row lands after the boundary.
    if (isCompactionBoundary(msg)) return null;
    if (msg.role !== 'assistant') continue;
    const used = msg.context_tokens ?? 0;
    if (used > 0) return used;
  }
  return null;
}

/**
 * How full the model's context is. Null when the window is unknown or no turn has reported
 * usage yet — the caller renders nothing rather than a percentage of a guessed capacity.
 */
export function contextUsage(
  messages: Message[],
  contextWindow: number | null | undefined
): ContextUsage | null {
  if (!contextWindow || contextWindow <= 0) return null;
  const used = lastPromptTokens(messages);
  if (used === null) return null;
  return { used, window: contextWindow, percent: Math.min(100, (used / contextWindow) * 100) };
}

/**
 * Whether to offer compaction: real pressure AND something left to compact.
 *
 * Works with or without a known window (see `COMPACT_OFFER_TOKENS`), and goes quiet once
 * the transcript holds nothing the server would summarize — otherwise the button survives
 * its own compaction, because the summary turn's own prompt was the full pre-compaction
 * context and the gauge still reads the peak.
 */
export function shouldOfferCompaction(
  messages: Message[],
  contextWindow: number | null | undefined
): boolean {
  if (!hasCompactableMessages(messages)) return false;
  const usage = contextUsage(messages, contextWindow);
  if (usage) return usage.percent >= COMPACT_OFFER_PERCENT;
  const used = lastPromptTokens(messages);
  return used !== null && used >= COMPACT_OFFER_TOKENS;
}

/** Whether any row after the last compaction boundary would actually be summarized. */
function hasCompactableMessages(messages: Message[]): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (isCompactionBoundary(msg)) return false;
    if (msg.role !== 'system') return true;
  }
  return false;
}

/**
 * Whether a row is a compaction boundary.
 *
 * Deliberately looser than the backend predicate, which also requires a non-empty summary:
 * the API strips that field before the row reaches any client, so the check is impossible
 * here. The server never writes a summary-less boundary, so the two agree in practice.
 */
export function isCompactionBoundary(msg: Message): boolean {
  const content = msg.content;
  return (
    msg.role === 'system' &&
    typeof content === 'object' &&
    !Array.isArray(content) &&
    content.type === 'session_event' &&
    content.subject === 'context' &&
    content.action === 'compacted'
  );
}
