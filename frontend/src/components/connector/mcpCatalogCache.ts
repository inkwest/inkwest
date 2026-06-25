import { getMcpCatalog, type McpCatalog } from '@/services/api';

// The MCP catalog rarely changes (an admin adds a server now and then), but McpPanel
// remounts on every session switch / composer remount. Without a cache each remount
// refetched and the toolbar icon flickered (absent → present). Cache it process-wide
// with a short TTL; McpPanel seeds its initial state from `peekMcpCatalog()` so a
// remount renders the known catalog synchronously (no flicker), and calls
// `loadMcpCatalog(true)` to force a refresh after a change.
let cache: { data: McpCatalog; at: number } | null = null;
const TTL_MS = 5 * 60 * 1000;

export async function loadMcpCatalog(force = false): Promise<McpCatalog> {
  if (!force && cache && Date.now() - cache.at < TTL_MS) return cache.data;
  const data = await getMcpCatalog();
  cache = { data, at: Date.now() };
  return data;
}

/** Synchronous, non-stale cached catalog (or null) — for seeding initial state. */
export function peekMcpCatalog(): McpCatalog | null {
  return cache && Date.now() - cache.at < TTL_MS ? cache.data : null;
}

/** Refresh the cache with a catalog already obtained from a mutation (e.g. an OAuth
 *  disconnect returns the fresh catalog) so a later remount doesn't show stale state. */
export function primeMcpCatalog(data: McpCatalog): void {
  cache = { data, at: Date.now() };
}

/** Test seam: clear the cache so each test starts cold. */
export function resetMcpCatalogCache(): void {
  cache = null;
}
