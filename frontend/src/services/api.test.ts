import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  wsBaseUrl,
  getClusters,
  createCluster,
  getSessions,
  createSession,
  getSession,
  deleteSession,
  updateSession,
  getFailedMessages,
  getEffectiveSkills,
  searchGlobal,
  fetchWsTicket,
  decideMcpAsk,
  uploadAttachments,
  fetchAttachmentBlob,
} from './api';
import { useBackendStore } from '@/store/backend';

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  });
}

function errorResponse(status: number) {
  return Promise.resolve({
    ok: false,
    status,
    json: () => Promise.resolve({ detail: 'error' }),
  });
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe('getClusters', () => {
  it('returns cluster items', async () => {
    const clusters = [{ id: '1', name: 'c1', display_name: 'Cluster 1' }];
    mockFetch.mockReturnValue(jsonResponse({ items: clusters, total: 1 }));

    const result = await getClusters();
    expect(result).toEqual(clusters);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/clusters'),
      expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }),
    );
  });
});

describe('createCluster', () => {
  it('sends POST with cluster data', async () => {
    const cluster = { id: '1', name: 'new', display_name: 'New' };
    mockFetch.mockReturnValue(jsonResponse(cluster, 201));

    const result = await createCluster({
      name: 'new',
      display_name: 'New',
      kubeconfig_path: '/tmp/kc',
    });
    expect(result).toEqual(cluster);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/clusters'),
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('getSessions', () => {
  it('returns the paginated envelope', async () => {
    const sessions = [{ id: 's1', cluster_id: 'c1', title: 'Test' }];
    mockFetch.mockReturnValue(
      jsonResponse({ items: sessions, total: 1, page: 1, page_size: 20 }),
    );

    const result = await getSessions();
    expect(result).toEqual({ items: sessions, total: 1, page: 1, page_size: 20 });
  });

  it('encodes sort/order and repeats the share filter as query params', async () => {
    mockFetch.mockReturnValue(jsonResponse({ items: [], total: 0, page: 1, page_size: 20 }));
    await getSessions({ sort: 'activity', order: 'asc', share: ['shared_with_me', 'private'] });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('sort=activity');
    expect(url).toContain('order=asc');
    expect(url).toContain('share=shared_with_me');
    expect(url).toContain('share=private');
  });
});

describe('createSession', () => {
  it('sends POST with cluster_id', async () => {
    const session = { id: 's1', cluster_id: 'c1' };
    mockFetch.mockReturnValue(jsonResponse(session, 201));

    const result = await createSession({ cluster_id: 'c1' });
    expect(result).toEqual(session);
  });
});

describe('getSession', () => {
  it('fetches session with messages', async () => {
    const session = { id: 's1', messages: [] };
    mockFetch.mockReturnValue(jsonResponse(session));

    const result = await getSession('s1');
    expect(result).toEqual(session);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/sessions/s1'),
      expect.any(Object),
    );
  });
});

describe('deleteSession', () => {
  it('sends DELETE request', async () => {
    mockFetch.mockReturnValue(jsonResponse(null, 204));

    await deleteSession('s1');
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/sessions/s1'),
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});

describe('updateSession', () => {
  it('sends PATCH with title', async () => {
    const session = { id: 's1', title: 'Updated' };
    mockFetch.mockReturnValue(jsonResponse(session));

    const result = await updateSession('s1', { title: 'Updated' });
    expect(result).toEqual(session);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/sessions/s1'),
      expect.objectContaining({ method: 'PATCH' }),
    );
  });
});

describe('getFailedMessages', () => {
  it('returns failed messages array', async () => {
    mockFetch.mockReturnValue(jsonResponse([]));

    const result = await getFailedMessages('s1');
    expect(result).toEqual([]);
  });
});

describe('getEffectiveSkills', () => {
  it('reads the per-user effective set, not the admin catalog', async () => {
    const skills = [{ id: '1', name: 'mine', slug: 'mine', content: 'x', is_active: true }];
    mockFetch.mockReturnValue(jsonResponse({ items: skills, total: 1 }));

    const result = await getEffectiveSkills();
    expect(result).toEqual(skills);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/me/skills/effective'),
      expect.any(Object),
    );
  });
});

describe('searchGlobal', () => {
  it('passes encoded query', async () => {
    const response = { query: 'test query', results: [], total: 0 };
    mockFetch.mockReturnValue(jsonResponse(response));

    const result = await searchGlobal('test query');
    expect(result).toEqual(response);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('q=test%20query'),
      expect.any(Object),
    );
  });
});

describe('error handling', () => {
  it('throws with detail from error body', async () => {
    mockFetch.mockReturnValue(errorResponse(500));

    await expect(getClusters()).rejects.toThrow('error');
  });

  it('throws with detail on 404', async () => {
    mockFetch.mockReturnValue(errorResponse(404));

    await expect(getSession('nonexistent')).rejects.toThrow('error');
  });

  it('falls back to status code when body has no detail', async () => {
    mockFetch.mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 500,
        json: () => Promise.reject(new Error('not json')),
      }),
    );

    await expect(getClusters()).rejects.toThrow('API error: 500');
  });
});

describe('503 / backend-down retry', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0 });
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('retries a 503 then resolves, clearing the banner', async () => {
    const clusters = [{ id: '1', name: 'c1', display_name: 'C1' }];
    mockFetch
      .mockReturnValueOnce(errorResponse(503))
      .mockReturnValueOnce(jsonResponse({ items: clusters, total: 1 }));

    const p = getClusters();
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual(clusters);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(useBackendStore.getState().unavailable).toBe(false);
  });

  it('flags the backend unavailable while retrying', async () => {
    mockFetch
      .mockReturnValueOnce(errorResponse(503))
      .mockReturnValueOnce(errorResponse(503))
      .mockReturnValue(jsonResponse({ items: [], total: 0 }));

    const p = getClusters();
    // Let the first failed attempt schedule its backoff before asserting.
    await vi.advanceTimersByTimeAsync(0);
    expect(useBackendStore.getState().unavailable).toBe(true);

    await vi.runAllTimersAsync();
    await p;
    expect(useBackendStore.getState().unavailable).toBe(false);
  });

  it('retries mutations too (503 = request never processed)', async () => {
    const cluster = { id: '1', name: 'new', display_name: 'New' };
    mockFetch
      .mockReturnValueOnce(errorResponse(503))
      .mockReturnValueOnce(jsonResponse(cluster, 201));

    const p = createCluster({ name: 'new', display_name: 'New', kubeconfig_path: '/tmp/kc' });
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual(cluster);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch).toHaveBeenLastCalledWith(
      expect.any(String),
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('retries 502 and 504 on mutations too (accepted trade-off)', async () => {
    const cluster = { id: '1', name: 'new', display_name: 'New' };
    mockFetch
      .mockReturnValueOnce(errorResponse(502))
      .mockReturnValueOnce(errorResponse(504))
      .mockReturnValueOnce(jsonResponse(cluster, 201));

    const p = createCluster({ name: 'new', display_name: 'New', kubeconfig_path: '/tmp/kc' });
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual(cluster);
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it('times out a hung attempt and retries', async () => {
    // First attempt never responds until aborted; the timeout fires and we retry.
    mockFetch
      .mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            );
          }),
      )
      .mockReturnValueOnce(jsonResponse({ items: [], total: 0 }));

    const p = getSessions();
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual({ items: [], total: 0 });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('does not abort/replay a mutation before its (long) fail-timeout', async () => {
    // A POST may still be running server-side (slow AWS enumerate/STS mint); the
    // client must not abort+retry it on the read timeout, or it could run twice.
    let aborted = false;
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            aborted = true;
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    );

    const p = createCluster({ name: 'x', display_name: 'X', kubeconfig_path: '/k' });
    let settled = false;
    void p.then(
      () => (settled = true),
      () => (settled = true),
    );

    // Advance past the GET read timeout (30s) — a GET would have aborted+retried;
    // a mutation must still be pending and untouched, well under the 5-min ceiling.
    await vi.advanceTimersByTimeAsync(60000);

    expect(aborted).toBe(false);
    expect(settled).toBe(false);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('fails a hung mutation after the fail-timeout WITHOUT replaying it', async () => {
    let aborted = false;
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            aborted = true;
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    );

    const p = createCluster({ name: 'x', display_name: 'X', kubeconfig_path: '/k' });
    const expectation = expect(p).rejects.toThrow();
    // Past the 5-min mutation fail-timeout.
    await vi.advanceTimersByTimeAsync(300001);
    await expectation;

    expect(aborted).toBe(true);
    // Crucially: failed, never retried — a hung mutation is surfaced, not replayed.
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('does not retry when the caller aborts (navigation/unmount)', async () => {
    const controller = new AbortController();
    mockFetch.mockImplementationOnce(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );

    const p = fetchWsTicket(controller.signal);
    controller.abort();

    await expect(p).rejects.toThrow();
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('retries a network failure then resolves', async () => {
    mockFetch
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockReturnValueOnce(jsonResponse({ items: [], total: 0 }));

    const p = getSessions();
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual({ items: [], total: 0 });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('retries a mutation on a network failure too (accepted replay trade-off)', async () => {
    const cluster = { id: '1', name: 'new', display_name: 'New' };
    mockFetch
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockReturnValueOnce(jsonResponse(cluster, 201));

    const p = createCluster({ name: 'new', display_name: 'New', kubeconfig_path: '/tmp/kc' });
    await vi.runAllTimersAsync();

    await expect(p).resolves.toEqual(cluster);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('gives up after the retry cap and surfaces the error', async () => {
    mockFetch.mockReturnValue(errorResponse(503));

    const p = getClusters();
    // Promise rejects after exhausting retries; assert before flushing real time.
    const expectation = expect(p).rejects.toThrow('error');
    await vi.runAllTimersAsync();
    await expectation;
    // 1 initial + MAX_RETRIES backed-off attempts.
    expect(mockFetch).toHaveBeenCalledTimes(13);
    // On exhaustion this call releases its banner hold (refcount → 0), so a lone
    // exhausted call doesn't leave the banner stuck on with no holder. During a
    // real outage other in-flight retriers / WS reconnects keep it up.
    expect(useBackendStore.getState().unavailable).toBe(false);
    // A gateway status returned after exhaustion means "unreachable" (amber banner
    // territory) — it must NOT land in the red 5xx strip.
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });

  it('does not call fetch when the caller signal is already aborted', async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(fetchWsTicket(controller.signal)).rejects.toThrow();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('central 5xx reporting', () => {
  beforeEach(() => {
    useBackendStore.setState({ unavailable: false, retrying: 0, serverErrors: {} });
  });

  it('reports an app-emitted 500 keyed METHOD /path (UUIDs collapsed) before throwing', async () => {
    mockFetch.mockReturnValueOnce(errorResponse(500));
    await expect(getSession('123e4567-e89b-4d3c-8456-426614174000')).rejects.toThrow('error');
    expect(useBackendStore.getState().serverErrors).toEqual({
      'GET /api/sessions/:id': { status: 500, message: 'error', count: 1 },
    });
  });

  it('does not report 4xx errors', async () => {
    mockFetch.mockReturnValueOnce(errorResponse(404));
    await expect(getClusters()).rejects.toThrow('error');
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });

  it('self-heals the strip entry when the same route succeeds again', async () => {
    useBackendStore.getState().reportServerError('GET /api/clusters', 500, 'boom');
    mockFetch.mockReturnValueOnce(jsonResponse([]));
    await getClusters();
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });

  it('collapses opaque token path segments (ask ids) in the key', async () => {
    mockFetch.mockReturnValueOnce(errorResponse(500));
    // token_urlsafe(16) shape: 22 url-safe chars — must not mint one key per ask.
    await expect(decideMcpAsk('Ab3dEf6hIj9kLm2nOp5qRs', 'allow')).rejects.toThrow('error');
    expect(Object.keys(useBackendStore.getState().serverErrors)).toEqual(['POST /api/mcp/ask/:id']);
  });

  it('attachment upload mirrors the report and self-heals on the next success', async () => {
    const sid = '123e4567-e89b-4d3c-8456-426614174000';
    mockFetch.mockReturnValueOnce(errorResponse(500));
    await expect(uploadAttachments(sid, [new File(['x'], 'a.txt')])).rejects.toThrow('error');
    expect(Object.keys(useBackendStore.getState().serverErrors)).toEqual([
      'POST /api/sessions/:id/attachments',
    ]);
    mockFetch.mockReturnValueOnce(jsonResponse([]));
    await uploadAttachments(sid, [new File(['x'], 'a.txt')]);
    expect(useBackendStore.getState().serverErrors).toEqual({});
  });

  it('attachment blob failures dedupe under a :file placeholder (no per-filename fan-out)', async () => {
    const sid = '123e4567-e89b-4d3c-8456-426614174000';
    mockFetch.mockReturnValue(errorResponse(500));
    await expect(fetchAttachmentBlob(sid, 'one.png')).rejects.toThrow('Failed to load attachment');
    await expect(fetchAttachmentBlob(sid, 'two.png')).rejects.toThrow('Failed to load attachment');
    const errs = useBackendStore.getState().serverErrors;
    expect(Object.keys(errs)).toEqual(['GET /api/sessions/:id/attachments/:file']);
    expect(errs['GET /api/sessions/:id/attachments/:file'].count).toBe(2);
  });
});

describe('wsBaseUrl', () => {
  const env = import.meta.env as Record<string, string | undefined>;
  const original = env.VITE_WS_URL;

  afterEach(() => {
    if (original === undefined) {
      delete env.VITE_WS_URL;
    } else {
      env.VITE_WS_URL = original;
    }
  });

  it('returns the configured URL when VITE_WS_URL is set', () => {
    env.VITE_WS_URL = 'wss://chat.example.com';
    expect(wsBaseUrl()).toBe('wss://chat.example.com');
  });

  it('derives a same-origin URL when VITE_WS_URL is empty (combined image)', () => {
    env.VITE_WS_URL = '';
    const expected = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;
    expect(wsBaseUrl()).toBe(expected);
  });

  it('falls back to the dev backend when VITE_WS_URL is unset', () => {
    delete env.VITE_WS_URL;
    expect(wsBaseUrl()).toBe('ws://localhost:8000');
  });
});
