import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getClusters,
  createCluster,
  getSessions,
  createSession,
  getSession,
  deleteSession,
  updateSession,
  retryMessage,
  getFailedMessages,
  searchGlobal,
} from './api';

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
  it('returns session items', async () => {
    const sessions = [{ id: 's1', cluster_id: 'c1', title: 'Test' }];
    mockFetch.mockReturnValue(
      jsonResponse({ items: sessions, total: 1, page: 1, page_size: 20 }),
    );

    const result = await getSessions();
    expect(result).toEqual(sessions);
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

describe('retryMessage', () => {
  it('sends POST to retry endpoint', async () => {
    const msg = { id: 'm1', status: 'pending' };
    mockFetch.mockReturnValue(jsonResponse(msg));

    const result = await retryMessage('s1', 'm1');
    expect(result).toEqual(msg);
  });
});

describe('getFailedMessages', () => {
  it('returns failed messages array', async () => {
    mockFetch.mockReturnValue(jsonResponse([]));

    const result = await getFailedMessages('s1');
    expect(result).toEqual([]);
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
        status: 502,
        json: () => Promise.reject(new Error('not json')),
      }),
    );

    await expect(getClusters()).rejects.toThrow('API error: 502');
  });
});
