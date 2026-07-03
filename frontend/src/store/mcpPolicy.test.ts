import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/services/api', () => ({
  getMyMcpPolicy: vi.fn(),
  getSessionMcpPolicy: vi.fn(),
  setMyMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
  setSessionMcpPolicy: vi.fn().mockResolvedValue({ policies: [] }),
}));

import {
  getMyMcpPolicy,
  getSessionMcpPolicy,
  setMyMcpPolicy,
  setSessionMcpPolicy,
} from '@/services/api';
import {
  persistToolVerdict,
  resetPolicySaveChainForTests,
  runPolicySave,
  useMcpPolicyVersion,
} from './mcpPolicy';

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('runPolicySave', () => {
  beforeEach(() => resetPolicySaveChainForTests());

  it('serializes saves in enqueue order even when the first is slower', async () => {
    const order: string[] = [];
    const first = deferred<void>();
    const p1 = runPolicySave(async () => {
      await first.promise;
      order.push('first');
    });
    const p2 = runPolicySave(async () => {
      order.push('second');
    });
    // Second is enqueued but must not start until the first settles.
    await Promise.resolve();
    expect(order).toEqual([]);
    first.resolve();
    await Promise.all([p1, p2]);
    expect(order).toEqual(['first', 'second']);
  });

  it('a rejected save does not wedge the chain — the next save still runs', async () => {
    await expect(runPolicySave(() => Promise.reject(new Error('boom')))).rejects.toThrow('boom');
    await expect(runPolicySave(() => Promise.resolve('ok'))).resolves.toBe('ok');
  });
});

describe('persistToolVerdict', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetPolicySaveChainForTests();
  });

  it('global scope: merges into the current global entries and bumps the global version', async () => {
    vi.mocked(getMyMcpPolicy).mockResolvedValue({
      policies: [
        { server_key: 'k', tool: '*', verdict: 'ask' },
        { server_key: 'other', tool: 'x', verdict: 'deny' },
      ],
    });
    const before = useMcpPolicyVersion.getState().global;
    await persistToolVerdict({ scope: 'global' }, 'k', 'query', 'allow');
    // Sibling '*' row survives (full-state PUT); other servers' rows are filtered out.
    expect(setMyMcpPolicy).toHaveBeenCalledWith('k', [
      { tool: '*', verdict: 'ask' },
      { tool: 'query', verdict: 'allow' },
    ]);
    expect(useMcpPolicyVersion.getState().global).toBe(before + 1);
  });

  it('session scope: merges into the conversation entries and bumps the session version', async () => {
    vi.mocked(getSessionMcpPolicy).mockResolvedValue({
      policies: [{ server_key: 'k', tool: 'other-tool', verdict: 'allow' }],
    });
    const before = useMcpPolicyVersion.getState().session;
    await persistToolVerdict({ scope: 'session', sessionId: 's1' }, 'k', 'query', 'deny');
    expect(setSessionMcpPolicy).toHaveBeenCalledWith('s1', 'k', [
      { tool: 'other-tool', verdict: 'allow' },
      { tool: 'query', verdict: 'deny' },
    ]);
    expect(useMcpPolicyVersion.getState().session).toBe(before + 1);
  });

  it('propagates a failed write (no version bump) — best-effort is the CALLER\'s call', async () => {
    vi.mocked(getMyMcpPolicy).mockRejectedValue(new Error('500'));
    const before = useMcpPolicyVersion.getState().global;
    await expect(persistToolVerdict({ scope: 'global' }, 'k', 'q', 'deny')).rejects.toThrow('500');
    expect(setMyMcpPolicy).not.toHaveBeenCalled();
    expect(useMcpPolicyVersion.getState().global).toBe(before);
  });
});
