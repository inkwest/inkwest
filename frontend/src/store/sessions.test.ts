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

describe('sessionsStore sharing', () => {
  it('setShareCount updates the matching list item and currentSession', () => {
    useSessionsStore.setState({
      sessions: [session('a'), session('b')],
      currentSession: { ...session('a'), messages: [], cluster: null },
    });
    useSessionsStore.getState().setShareCount('a', 3);
    expect(useSessionsStore.getState().sessions.find((s) => s.id === 'a')?.share_count).toBe(3);
    expect(useSessionsStore.getState().currentSession?.share_count).toBe(3);
    // Untouched sessions are left alone.
    expect(useSessionsStore.getState().sessions.find((s) => s.id === 'b')?.share_count).toBeUndefined();
  });

  it('setShareCount leaves currentSession alone when ids differ', () => {
    useSessionsStore.setState({
      sessions: [session('a')],
      currentSession: { ...session('b'), messages: [], cluster: null },
    });
    useSessionsStore.getState().setShareCount('a', 5);
    expect(useSessionsStore.getState().currentSession?.share_count).toBeUndefined();
  });
});

describe('sessionsStore ensureSessionTitle', () => {
  function untitled(id: string): Session {
    return { ...session(id), title: undefined } as Session;
  }

  it('titles an untitled session from its first message', () => {
    useSessionsStore.setState({ sessions: [untitled('a')], currentSession: null });
    useSessionsStore.getState().ensureSessionTitle('a', '  Investigate the GitLab 500s  ');
    expect(useSessionsStore.getState().sessions[0].title).toBe('Investigate the GitLab 500s');
  });

  it('truncates a long message at a word boundary (mirrors the backend)', () => {
    useSessionsStore.setState({ sessions: [untitled('a')], currentSession: null });
    const long =
      'Why are the production payment workers crashing during the nightly batch reconciliation job';
    useSessionsStore.getState().ensureSessionTitle('a', long);
    const title = useSessionsStore.getState().sessions[0].title!;
    expect(title.endsWith('...')).toBe(true);
    expect(title.length).toBeLessThanOrEqual(63);
    expect(long.startsWith(title.slice(0, -3))).toBe(true);
  });

  it('is a no-op once the session already has a title', () => {
    useSessionsStore.setState({ sessions: [session('a')], currentSession: null });
    useSessionsStore.getState().ensureSessionTitle('a', 'a different first message');
    expect(useSessionsStore.getState().sessions[0].title).toBe('a');
  });

  it('is a no-op for an unknown id or empty message', () => {
    useSessionsStore.setState({ sessions: [untitled('a')], currentSession: null });
    useSessionsStore.getState().ensureSessionTitle('zzz', 'hello');
    useSessionsStore.getState().ensureSessionTitle('a', '   ');
    expect(useSessionsStore.getState().sessions[0].title).toBeUndefined();
  });

  it('also titles currentSession when ids match', () => {
    useSessionsStore.setState({
      sessions: [untitled('a')],
      currentSession: { ...untitled('a'), messages: [], cluster: null },
    });
    useSessionsStore.getState().ensureSessionTitle('a', 'first message');
    expect(useSessionsStore.getState().currentSession?.title).toBe('first message');
  });
});
