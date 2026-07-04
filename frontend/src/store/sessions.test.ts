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
    // Restore the real action afterward so later tests don't inherit the stub
    // (store state, unlike vi mocks, isn't reset by restoreAllMocks).
    const realFetch = useSessionsStore.getState().fetchSessions;
    const fetchSpy = vi.fn();
    useSessionsStore.setState({ fetchSessions: fetchSpy });
    await useSessionsStore.getState().persistSessionOrder();
    expect(fetchSpy).toHaveBeenCalled();
    useSessionsStore.setState({ fetchSessions: realFetch });
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

describe('sessionsStore sort/filter + pagination', () => {
  const envelope = (ids: string[], total: number, page = 1) => ({
    items: ids.map(session),
    total,
    page,
    page_size: 20,
  });

  beforeEach(() => {
    vi.restoreAllMocks();
    useSessionsStore.setState({
      sessions: [],
      total: 0,
      loadedPages: 1,
      isLoadingMore: false,
      sortMode: 'manual',
      sortOrder: 'desc',
      filters: [],
      dateRange: 'all',
      customFrom: '',
      customTo: '',
    });
  });

  it('fetchSessions forwards the current sort/order/filters and stores items + total', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope(['a', 'b'], 2));
    useSessionsStore.setState({ sortMode: 'activity', sortOrder: 'asc', filters: ['shared_with_me'] });
    await useSessionsStore.getState().fetchSessions();
    expect(spy).toHaveBeenCalledWith({
      page: 1,
      page_size: 20,
      sort: 'activity',
      order: 'asc',
      share: ['shared_with_me'],
    });
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a', 'b']);
    expect(useSessionsStore.getState().total).toBe(2);
  });

  it('loadMoreSessions appends the next page and advances loadedPages', async () => {
    useSessionsStore.setState({ sessions: [session('a'), session('b')], total: 3, loadedPages: 1 });
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope(['c'], 3, 2));
    await useSessionsStore.getState().loadMoreSessions();
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a', 'b', 'c']);
    expect(useSessionsStore.getState().loadedPages).toBe(2);
  });

  it('loadMoreSessions is a no-op once everything is loaded', async () => {
    useSessionsStore.setState({ sessions: [session('a')], total: 1 });
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 1));
    await useSessionsStore.getState().loadMoreSessions();
    expect(spy).not.toHaveBeenCalled();
  });

  it('fetchSessions reloads all loaded pages so a load-more expansion survives a refresh', async () => {
    useSessionsStore.setState({ sessions: [session('a'), session('b')], total: 4, loadedPages: 2 });
    const spy = vi
      .spyOn(api, 'getSessions')
      .mockResolvedValueOnce(envelope(['a', 'b'], 4, 1))
      .mockResolvedValueOnce(envelope(['c', 'd'], 4, 2));
    await useSessionsStore.getState().fetchSessions();
    expect(spy).toHaveBeenCalledTimes(2);
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('toggleFilter flips a sharing filter and refetches from page 1', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    useSessionsStore.getState().toggleFilter('private');
    expect(useSessionsStore.getState().filters).toEqual(['private']);
    expect(useSessionsStore.getState().loadedPages).toBe(1);
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ share: ['private'], page: 1 }));
    useSessionsStore.getState().toggleFilter('private');
    expect(useSessionsStore.getState().filters).toEqual([]);
  });

  it('setFilters replaces the whole set; all-selected collapses to no filter on the wire', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    // Selecting every bucket ≡ no filter, so the request sends share: [].
    useSessionsStore.getState().setFilters(['shared_with_me', 'shared_by_me', 'private']);
    expect(useSessionsStore.getState().filters).toEqual(['shared_with_me', 'shared_by_me', 'private']);
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ share: [], page: 1 }));
    // A proper subset is sent verbatim.
    useSessionsStore.getState().setFilters(['private']);
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ share: ['private'] }));
  });

  it('setDateRange sends an activity_after cutoff, and "all" clears it', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    useSessionsStore.getState().setDateRange('7d');
    expect(useSessionsStore.getState().dateRange).toBe('7d');
    expect(typeof spy.mock.calls[0]?.[0]?.activity_after).toBe('string');
    useSessionsStore.getState().setDateRange('all');
    expect(spy.mock.calls.at(-1)?.[0]?.activity_after).toBeUndefined();
  });

  it('setCustomRange sends both from/to ISO bounds with from < to', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    useSessionsStore.getState().setCustomRange('2026-06-01', '2026-06-30');
    expect(useSessionsStore.getState().dateRange).toBe('custom');
    const params = spy.mock.calls.at(-1)?.[0];
    // TZ-independent: both bounds are valid ISO instants and ordered (from start-of-day
    // to end-of-day). Asserting the exact date text would break off the UTC-boundary.
    expect(new Date(params!.activity_after!).getTime()).toBeLessThan(
      new Date(params!.activity_before!).getTime()
    );
  });

  it('custom range with only a "from" leaves the upper bound open', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    useSessionsStore.getState().setCustomRange('2026-06-01', '');
    const params = spy.mock.calls.at(-1)?.[0];
    expect(typeof params?.activity_after).toBe('string');
    expect(params?.activity_before).toBeUndefined();
  });

  it('does not flash the Loading gate when refetching from an empty filtered result', async () => {
    // Empty *result* ≠ never loaded: after a filter that matched nothing, switching to
    // one that matches must swap in place, not unmount the sidebar behind "Loading...".
    useSessionsStore.setState({ sessions: [], hasLoaded: true, isLoading: false });
    let resolve!: (v: ReturnType<typeof envelope>) => void;
    const deferred = new Promise<ReturnType<typeof envelope>>((r) => {
      resolve = r;
    });
    vi.spyOn(api, 'getSessions').mockReturnValueOnce(deferred as never);
    const p = useSessionsStore.getState().fetchSessions();
    expect(useSessionsStore.getState().isLoading).toBe(false); // mid-flight: no gate
    resolve(envelope(['a'], 1));
    await p;
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a']);
  });

  it('drops a stale fetch that resolves after a newer one (rapid sort toggle race)', async () => {
    // Fetch A (old sort) is slow; fetch B (new sort) starts later but resolves first.
    // Without the token guard, A's late resolution would overwrite B's fresh list.
    let resolveA!: (v: ReturnType<typeof envelope>) => void;
    const slowA = new Promise<ReturnType<typeof envelope>>((r) => {
      resolveA = r;
    });
    const spy = vi.spyOn(api, 'getSessions');
    spy.mockReturnValueOnce(slowA as never);
    const pA = useSessionsStore.getState().fetchSessions();
    spy.mockResolvedValueOnce(envelope(['fresh'], 1));
    const pB = useSessionsStore.getState().fetchSessions();
    await pB;
    resolveA(envelope(['stale'], 1));
    await pA;
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['fresh']);
  });

  it('ignores an unparsable persisted custom date instead of throwing', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope(['a'], 1));
    useSessionsStore.setState({ dateRange: 'custom', customFrom: 'not-a-date', customTo: '' });
    await useSessionsStore.getState().fetchSessions();
    // The bad bound is skipped (not sent, no RangeError) and the fetch still lands.
    expect(spy.mock.calls.at(-1)?.[0]?.activity_after).toBeUndefined();
    expect(useSessionsStore.getState().sessions.map((s) => s.id)).toEqual(['a']);
  });

  it('resetView restores every view field to its default and refetches', async () => {
    const spy = vi.spyOn(api, 'getSessions').mockResolvedValue(envelope([], 0));
    useSessionsStore.setState({
      sortMode: 'created',
      sortOrder: 'asc',
      filters: ['private'],
      dateRange: 'custom',
      customFrom: '2026-06-01',
      customTo: '2026-06-30',
    });
    useSessionsStore.getState().resetView();
    const s = useSessionsStore.getState();
    expect(s.sortMode).toBe('activity');
    expect(s.sortOrder).toBe('desc');
    // Default = all buckets selected (boxes checked); collapses to share: [] on the wire.
    expect(s.filters).toEqual(['shared_with_me', 'shared_by_me', 'private']);
    expect(s.dateRange).toBe('all');
    expect(s.customFrom).toBe('');
    expect(s.customTo).toBe('');
    const params = spy.mock.calls.at(-1)?.[0];
    expect(params).toMatchObject({ sort: 'activity', order: 'desc', share: [] });
    expect(params?.activity_after).toBeUndefined();
    expect(params?.activity_before).toBeUndefined();
  });
});

describe('sessionsStore updateSessionTokens', () => {
  const base: Session = {
    id: 's1',
    total_input_tokens: 1000,
    total_output_tokens: 500,
    total_cache_read_tokens: 111,
    total_cache_write_tokens: 22,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as Session;

  beforeEach(() => {
    useSessionsStore.setState({ sessions: [base], currentSession: null });
  });

  it('accumulates the turn usage onto the fetched cumulative totals', () => {
    useSessionsStore.getState().updateSessionTokens('s1', 10, 5, 27082, 3242);
    const s = useSessionsStore.getState().sessions[0];
    // badges show the CONVERSATION total, not the last turn
    expect(s.total_input_tokens).toBe(1010);
    expect(s.total_output_tokens).toBe(505);
    expect(s.total_cache_read_tokens).toBe(111 + 27082);
    expect(s.total_cache_write_tokens).toBe(22 + 3242);
  });

  it('a fully-cached turn (0 fresh in/out) keeps the cumulative and adds the cache', () => {
    useSessionsStore.getState().updateSessionTokens('s1', 0, 0, 5000, undefined);
    const s = useSessionsStore.getState().sessions[0];
    expect(s.total_input_tokens).toBe(1000);
    expect(s.total_output_tokens).toBe(500);
    expect(s.total_cache_read_tokens).toBe(111 + 5000);
    expect(s.total_cache_write_tokens).toBe(22);
  });

  it('accumulates across consecutive turns', () => {
    useSessionsStore.getState().updateSessionTokens('s1', 10, 5, 100, 0);
    useSessionsStore.getState().updateSessionTokens('s1', 20, 8, 0, 50);
    const s = useSessionsStore.getState().sessions[0];
    expect(s.total_input_tokens).toBe(1030);
    expect(s.total_output_tokens).toBe(513);
    expect(s.total_cache_read_tokens).toBe(211);
    expect(s.total_cache_write_tokens).toBe(72);
  });
});
