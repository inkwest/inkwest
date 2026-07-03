import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('@/services/api', () => ({
  decideMcpAsk: vi.fn().mockResolvedValue(undefined),
}));

import { decideMcpAsk } from '@/services/api';
import { useMcpStore } from './mcp';

const ASK = { askId: 'a1', mcpKey: 'notion', tool: 'search', sessionId: 's1' };

describe('useMcpStore', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMcpStore.setState({ authRequired: [], pendingAsks: [], claimedAsks: {} });
  });

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

  it('claimAsk/releaseAsk toggle the claim; repeats are no-ops', () => {
    useMcpStore.getState().claimAsk('a1');
    useMcpStore.getState().claimAsk('a1');
    expect(useMcpStore.getState().claimedAsks).toEqual({ a1: true });
    useMcpStore.getState().releaseAsk('a1');
    useMcpStore.getState().releaseAsk('a1');
    expect(useMcpStore.getState().claimedAsks).toEqual({});
  });

  it('removeAsk drops the ask AND its claim (a stale claim would hide a re-added ask)', () => {
    useMcpStore.getState().addAsk(ASK);
    useMcpStore.getState().claimAsk('a1');
    useMcpStore.getState().removeAsk('a1');
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
    expect(useMcpStore.getState().claimedAsks).toEqual({});
  });

  it('decideAsk posts the verdict and removes the ask', async () => {
    useMcpStore.getState().addAsk(ASK);
    await useMcpStore.getState().decideAsk('a1', 'allow');
    expect(decideMcpAsk).toHaveBeenCalledWith('a1', 'allow');
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
  });

  it('decideAsk removes the ask even when the POST fails (server hold times out to deny)', async () => {
    vi.mocked(decideMcpAsk).mockRejectedValueOnce(new Error('404'));
    useMcpStore.getState().addAsk(ASK);
    useMcpStore.getState().claimAsk('a1');
    await useMcpStore.getState().decideAsk('a1', 'deny');
    expect(useMcpStore.getState().pendingAsks).toEqual([]);
    expect(useMcpStore.getState().claimedAsks).toEqual({});
  });
});
