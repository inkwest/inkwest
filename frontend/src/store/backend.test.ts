import { describe, it, expect, beforeEach } from 'vitest';
import { useBackendStore, MAX_SERVER_ERRORS } from './backend';

describe('useBackendStore', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false, serverErrors: {} });
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

  it('reportServerError dedupes by key, bumping the count and keeping the latest message', () => {
    const s = useBackendStore.getState();
    s.reportServerError('GET /api/x', 500, 'first');
    s.reportServerError('GET /api/x', 502, 'second');
    expect(useBackendStore.getState().serverErrors).toEqual({
      'GET /api/x': { status: 502, message: 'second', count: 2 },
    });
  });

  it('caps serverErrors, evicting the oldest key (a repeat bump keeps its slot)', () => {
    const s = useBackendStore.getState();
    for (let i = 0; i < MAX_SERVER_ERRORS + 2; i++) {
      s.reportServerError(`GET /api/e${i}`, 500, 'boom');
    }
    const keys = Object.keys(useBackendStore.getState().serverErrors);
    expect(keys).toHaveLength(MAX_SERVER_ERRORS);
    expect(keys).not.toContain('GET /api/e0');
    expect(keys).not.toContain('GET /api/e1');
    expect(keys).toContain(`GET /api/e${MAX_SERVER_ERRORS + 1}`);
  });

  it('clearServerError self-heals one key and is churn-free when absent', () => {
    const s = useBackendStore.getState();
    s.reportServerError('GET /api/x', 500, 'boom');
    s.reportServerError('GET /api/y', 500, 'boom');
    s.clearServerError('GET /api/x');
    expect(Object.keys(useBackendStore.getState().serverErrors)).toEqual(['GET /api/y']);
    let notifications = 0;
    const unsub = useBackendStore.subscribe(() => {
      notifications++;
    });
    s.clearServerError('GET /api/nope');
    expect(notifications).toBe(0);
    unsub();
  });

  it('dismissServerErrors clears the whole strip', () => {
    const s = useBackendStore.getState();
    s.reportServerError('GET /api/x', 500, 'boom');
    s.dismissServerErrors();
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });
});
