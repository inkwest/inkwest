import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useSessionsStore } from './sessions';
import * as api from '@/services/api';
import { Session } from '@/types/api';

function session(id: string): Session {
  return {
    id,
    cluster_id: null,
    title: id,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as Session;
}

describe('sessionsStore ordering', () => {
  beforeEach(() => {
    useSessionsStore.setState({ sessions: [session('a'), session('b'), session('c')] });
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('moveSession moves an item into the target slot (to the top)', () => {
    useSessionsStore.getState().moveSession('c', 'a');
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['c', 'a', 'b']);
  });

  it('moveSession moves an item downward past the target', () => {
    useSessionsStore.getState().moveSession('a', 'c');
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['b', 'c', 'a']);
  });

  it('moveSession is a no-op for unknown ids', () => {
    useSessionsStore.getState().moveSession('a', 'zzz');
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a', 'b', 'c']);
  });

  it('persistSessionOrder sends the current order to the API', async () => {
    const spy = vi.spyOn(api, 'reorderSessions').mockResolvedValue();
    useSessionsStore.getState().moveSession('c', 'a');
    await useSessionsStore.getState().persistSessionOrder();
    expect(spy).toHaveBeenCalledWith(['c', 'a', 'b']);
  });

  it('persistSessionOrder re-syncs from the server when saving fails', async () => {
    vi.spyOn(api, 'reorderSessions').mockRejectedValue(new Error('boom'));
    const fetchSpy = vi.fn();
    useSessionsStore.setState({ fetchSessions: fetchSpy });
    await useSessionsStore.getState().persistSessionOrder();
    expect(fetchSpy).toHaveBeenCalled();
  });
});
