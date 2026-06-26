// Capped exponential backoff with ±25% jitter, shared by the HTTP retry loop
// (services/api.ts) and the three WebSocket reconnect loops (ChatWebSocket,
// useSyncSocket, useEgressSocket) so the backoff band lives in exactly one place.
//
// The jitter matters: a deploy drops every client's socket at the same instant,
// so without it the whole fleet would retry in lockstep and stampede the backend
// the moment it comes back. The cap makes the delay plateau instead of growing
// unbounded across a long outage.
//
// `attempt` is 0-based (0 = first retry). Returns whole milliseconds for setTimeout.
export function jitteredBackoff(attempt: number, opts: { baseMs: number; maxMs: number }): number {
  const base = Math.min(opts.maxMs, opts.baseMs * 2 ** attempt);
  return Math.round(base * (0.75 + Math.random() * 0.5));
}

// Total wall-clock budget for WS reconnection before giving up: past this the
// backend looks permanently gone (not just bouncing through a deploy), so
// reconnecting forever would just burn battery and mask a dead session. Shared by
// all three WS reconnect loops.
export const RECONNECT_BUDGET_MS = 600_000; // 10 minutes

// Wall-clock give-up budget shared by the WS reconnect loops. `expired()` arms the
// deadline on its first call and returns true once spent; `reset()` (called on a
// successful open or when the tab returns to the foreground) grants a fresh budget
// so brief recoveries don't accumulate toward give-up. Caveat: it's wall-clock, so
// a backgrounded tab whose timers were throttled relies on the foreground reset to
// avoid giving up the instant the user returns.
export function createReconnectBudget(budgetMs: number) {
  let deadline: number | null = null;
  return {
    expired(): boolean {
      if (deadline === null) deadline = Date.now() + budgetMs;
      return Date.now() >= deadline;
    },
    reset(): void {
      deadline = null;
    },
  };
}
