import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useAnnouncementsStore } from './announcements';
import { useBackendStore } from './backend';
import type { Announcement } from '@/types/api';

// Real fetchAPI over a stubbed fetch, so the gateway/5xx-strip behavior is the production one.
const mockFetch = vi.fn();

const banner = (id: string, message = id): Announcement => ({
  id,
  message,
  level: 'info',
  color: null,
  text_color: null,
  opacity: 100,
  dismissible: true,
  ends_at: null,
  updated_at: '2026-10-28T10:00:00Z',
});

const json = (data: unknown, status = 200) =>
  Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(data) });

describe('useAnnouncementsStore', () => {
  beforeEach(() => {
    mockFetch.mockReset();
    vi.stubGlobal('fetch', mockFetch);
    useAnnouncementsStore.setState({ items: [], loaded: false });
    useBackendStore.setState({ unavailable: false, retrying: 0, gaveUp: false, serverErrors: {} });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('004-AC1: refresh loads the served banners and marks loaded', async () => {
    mockFetch.mockReturnValue(json([banner('a'), banner('b')]));
    await useAnnouncementsStore.getState().refresh();
    expect(mockFetch.mock.calls[0][0]).toMatch(/\/api\/announcements$/);
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['a', 'b']);
    expect(useAnnouncementsStore.getState().loaded).toBe(true);
  });

  it('004-AC11: an app error keeps the previous items and does not throw', async () => {
    useAnnouncementsStore.setState({ items: [banner('kept')], loaded: true });
    mockFetch.mockReturnValue(json({ detail: 'boom' }, 500));
    await expect(useAnnouncementsStore.getState().refresh()).resolves.toBeUndefined();
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['kept']);
  });

  it('004-AC11: a non-array body keeps the previous items', async () => {
    useAnnouncementsStore.setState({ items: [banner('kept')], loaded: true });
    mockFetch.mockReturnValue(json({ not: 'a list' }));
    await useAnnouncementsStore.getState().refresh();
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['kept']);
  });

  it('004-AC11: an exhausted gateway 503 keeps the items and never raises the 5xx strip', async () => {
    vi.useFakeTimers();
    useAnnouncementsStore.setState({ items: [banner('kept')], loaded: true });
    mockFetch.mockImplementation(() => json({ detail: 'down' }, 503));
    const done = useAnnouncementsStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(120_000);
    await done;
    expect(mockFetch.mock.calls.length).toBeGreaterThan(1); // it did retry
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['kept']);
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });

  it('a slower, older response does not overwrite a newer one', async () => {
    let releaseOld!: () => void;
    mockFetch
      .mockReturnValueOnce(new Promise((resolve) => (releaseOld = () => resolve(json([banner('old')])))))
      .mockReturnValueOnce(json([banner('new')]));
    const first = useAnnouncementsStore.getState().refresh();
    await useAnnouncementsStore.getState().refresh();
    releaseOld();
    await first;
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['new']);
  });

  it('an older response still applies when it lands first', async () => {
    let releaseNew!: () => void;
    mockFetch
      .mockReturnValueOnce(json([banner('old')]))
      .mockReturnValueOnce(new Promise((resolve) => (releaseNew = () => resolve(json([banner('new')])))));
    const first = useAnnouncementsStore.getState().refresh();
    const second = useAnnouncementsStore.getState().refresh();
    await first;
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['old']);
    releaseNew();
    await second;
    expect(useAnnouncementsStore.getState().items.map((a) => a.id)).toEqual(['new']);
  });
});
