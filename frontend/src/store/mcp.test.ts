import { describe, it, expect, beforeEach } from 'vitest';
import { useMcpStore } from './mcp';

describe('useMcpStore', () => {
  beforeEach(() => useMcpStore.setState({ authRequired: [] }));

  it('flags a key and dedupes repeats', () => {
    useMcpStore.getState().flagAuthRequired('notion');
    useMcpStore.getState().flagAuthRequired('notion');
    expect(useMcpStore.getState().authRequired).toEqual(['notion']);
  });

  it('clears only the given key', () => {
    useMcpStore.getState().flagAuthRequired('notion');
    useMcpStore.getState().flagAuthRequired('slack');
    useMcpStore.getState().clearAuthRequired('notion');
    expect(useMcpStore.getState().authRequired).toEqual(['slack']);
  });

  it('bumps the catalog version monotonically (drives McpPanel live reload)', () => {
    useMcpStore.setState({ catalogVersion: 0 });
    useMcpStore.getState().bumpCatalogVersion();
    useMcpStore.getState().bumpCatalogVersion();
    expect(useMcpStore.getState().catalogVersion).toBe(2);
  });
});
