import { useEffect, useRef, useState } from 'react';
import { create } from 'zustand';
import { currentPermission, enablePush, isPushSupported } from '@/services/push';
import { useConfigStore } from '@/store/config';

// Notification.permission has no change signal every browser fires, so whoever may flip it bumps
// this (plus the Permissions API `change` where supported): every opt-in surface re-renders and
// re-reads it, none keeps offering a stale "Turn on".
const usePermissionVersion = create<{ version: number }>(() => ({ version: 0 }));

export function bumpPushPermission(): void {
  usePermissionVersion.setState((s) => ({ version: s.version + 1 }));
}

/** Re-renders the caller whenever an opt-in surface may have changed permission or snooze. */
export function usePushPermissionVersion(): number {
  return usePermissionVersion((s) => s.version);
}

// Per browser on purpose: push subscriptions are per browser, not per account.
export const PUSH_OPT_IN_SNOOZE_KEY = 'inkwest.pushOptIn.snoozedUntil';
export const PUSH_OPT_IN_SNOOZE_MS = 30 * 24 * 60 * 60 * 1000;

export function isPushOptInSnoozed(): boolean {
  try {
    return Number(localStorage.getItem(PUSH_OPT_IN_SNOOZE_KEY) ?? 0) > Date.now();
  } catch {
    return false;
  }
}

/** Hides the in-app ask for 30 days on this browser, whichever surface the user answered from. */
export function snoozePushOptIn(): void {
  try {
    localStorage.setItem(PUSH_OPT_IN_SNOOZE_KEY, String(Date.now() + PUSH_OPT_IN_SNOOZE_MS));
  } catch {
    // Storage unavailable: the banner hides for this page load only.
  }
  bumpPushPermission();
}

export type PushOptInState = 'idle' | 'busy' | 'on' | 'dismissed' | 'error';

/** Shared state machine of the in-app asks (banner, invite popup); `enable` must run from a click. */
export function usePushOptIn() {
  const pushKey = useConfigStore((s) => s.config?.push_public_key ?? null);
  const version = usePushPermissionVersion();
  const [state, setState] = useState<PushOptInState>('idle');
  // The version this surface's own failure produced: any later bump (another surface acting)
  // supersedes the error: that surface now shows its own result, so this one's would be stale.
  const errorAt = useRef<number | null>(null);
  // Synchronous guard: a disabled button only helps once React has re-rendered.
  const inFlight = useRef(false);
  const permission = isPushSupported() ? currentPermission() : null;

  // Catches changes made outside this page: another tab, or the browser's site settings.
  useEffect(() => {
    if (!isPushSupported() || !navigator.permissions?.query) return;
    let status: PermissionStatus | undefined;
    let alive = true;
    navigator.permissions
      .query({ name: 'notifications' })
      .then((s) => {
        if (!alive) return;
        status = s;
        s.addEventListener('change', bumpPushPermission);
      })
      .catch(() => {});
    return () => {
      alive = false;
      status?.removeEventListener('change', bumpPushPermission);
    };
  }, []);

  const enable = async (): Promise<PushOptInState> => {
    if (inFlight.current) return 'busy';
    if (!pushKey) return 'idle';
    inFlight.current = true;
    setState('busy');
    let next: PushOptInState;
    try {
      await enablePush(pushKey);
      next = 'on';
    } catch {
      // A prompt closed unanswered leaves 'default': not a failure, the user just did not choose.
      // Never render the error itself: a 422 detail echoes the endpoint, a capability URL.
      next = currentPermission() === 'default' ? 'dismissed' : 'error';
    }
    // Re-asking right after a dismissal gets Chrome to auto-block, so it counts as "Not now" everywhere.
    if (next === 'dismissed') snoozePushOptIn();
    inFlight.current = false;
    setState(next);
    bumpPushPermission();
    errorAt.current = next === 'error' ? usePermissionVersion.getState().version : null;
    return next;
  };

  return {
    // Only while the browser was never asked: "granted but off" is the user's choice, "denied" is final.
    offer: !!pushKey && permission === 'default',
    permission,
    state: state === 'error' && errorAt.current !== version ? 'idle' : state,
    enable,
  };
}
