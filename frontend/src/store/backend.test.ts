import { describe, it, expect, beforeEach } from 'vitest';
import { useBackendStore } from './backend';

describe('useBackendStore', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false });
  });

  it('shows unavailable while at least one call is retrying', () => {
    const s = useBackendStore.getState();
    s.beginRetry();
    expect(useBackendStore.getState().unavailable).toBe(true);
    expect(useBackendStore.getState().retrying).toBe(1);
    s.endRetry();
    expect(useBackendStore.getState().unavailable).toBe(false);
    expect(useBackendStore.getState().retrying).toBe(0);
  });

  it('refcounts concurrent retriers — banner stays until the last one ends', () => {
    const s = useBackendStore.getState();
    s.beginRetry();
    s.beginRetry();
    s.endRetry();
    // One holder remains → banner stays up (no flapping off under concurrency).
    expect(useBackendStore.getState().unavailable).toBe(true);
    s.endRetry();
    expect(useBackendStore.getState().unavailable).toBe(false);
  });

  it('reachable() hard-clears regardless of outstanding holders', () => {
    const s = useBackendStore.getState();
    s.beginRetry();
    s.beginRetry();
    s.reachable();
    expect(useBackendStore.getState().unavailable).toBe(false);
    expect(useBackendStore.getState().retrying).toBe(0);
  });

  it('reachable() does NOT clear gaveUp (dead WS loops stay flagged until reload)', () => {
    const s = useBackendStore.getState();
    s.markGaveUp();
    s.beginRetry();
    s.reachable();
    // Banner state cleared, but gaveUp persists so the reload prompt survives an
    // HTTP success while the WS sockets remain dead.
    expect(useBackendStore.getState().unavailable).toBe(false);
    expect(useBackendStore.getState().retrying).toBe(0);
    expect(useBackendStore.getState().gaveUp).toBe(true);
  });

  it('endRetry never drives the count negative', () => {
    const s = useBackendStore.getState();
    s.endRetry();
    expect(useBackendStore.getState().retrying).toBe(0);
    expect(useBackendStore.getState().unavailable).toBe(false);
  });

  it('reachable() is a no-op (no subscriber churn) when already clear', () => {
    let notifications = 0;
    const unsub = useBackendStore.subscribe(() => {
      notifications++;
    });
    useBackendStore.getState().reachable();
    expect(notifications).toBe(0);
    unsub();
  });

  it('markGaveUp() is idempotent (no churn on repeated calls)', () => {
    const s = useBackendStore.getState();
    s.markGaveUp();
    expect(useBackendStore.getState().gaveUp).toBe(true);
    let notifications = 0;
    const unsub = useBackendStore.subscribe(() => {
      notifications++;
    });
    s.markGaveUp();
    expect(notifications).toBe(0);
    unsub();
  });
});
