import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@/services/api', () => ({
  getMcpCatalog: vi.fn(),
}));

import { getMcpCatalog } from '@/services/api';
import {
  loadMcpCatalog,
  peekMcpCatalog,
  primeMcpCatalog,
  resetMcpCatalogCache,
} from './mcpCatalogCache';

const mockGet = vi.mocked(getMcpCatalog);
const cat = (keys: string[]) => ({ keys, servers: [] });

describe('mcpCatalogCache', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetMcpCatalogCache();
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('peek is null until something populates the cache', () => {
    expect(peekMcpCatalog()).toBeNull();
  });

  it('loadMcpCatalog fetches once, then serves the cache within the TTL', async () => {
    mockGet.mockResolvedValue(cat(['a']));
    expect(await loadMcpCatalog()).toEqual(cat(['a']));
    expect(await loadMcpCatalog()).toEqual(cat(['a']));
    expect(mockGet).toHaveBeenCalledTimes(1); // second call served from cache
    expect(peekMcpCatalog()).toEqual(cat(['a']));
  });

  it('refetches once the TTL has elapsed', async () => {
    mockGet.mockResolvedValueOnce(cat(['a'])).mockResolvedValueOnce(cat(['b']));
    await loadMcpCatalog();
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000 + 5 * 60 * 1000 + 1); // TTL + 1ms
    expect(await loadMcpCatalog()).toEqual(cat(['b']));
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(peekMcpCatalog()).toEqual(cat(['b'])); // peek also sees stale-then-fresh
  });

  it('force bypasses the TTL', async () => {
    mockGet.mockResolvedValueOnce(cat(['a'])).mockResolvedValueOnce(cat(['b']));
    await loadMcpCatalog();
    expect(await loadMcpCatalog(true)).toEqual(cat(['b']));
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('peek returns null once the cached entry is stale', async () => {
    mockGet.mockResolvedValue(cat(['a']));
    await loadMcpCatalog();
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000 + 5 * 60 * 1000 + 1);
    expect(peekMcpCatalog()).toBeNull();
  });

  it('prime updates the cache without a fetch', () => {
    primeMcpCatalog(cat(['x']));
    expect(peekMcpCatalog()).toEqual(cat(['x']));
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('reset clears the cache', async () => {
    mockGet.mockResolvedValue(cat(['a']));
    await loadMcpCatalog();
    resetMcpCatalogCache();
    expect(peekMcpCatalog()).toBeNull();
  });
});
