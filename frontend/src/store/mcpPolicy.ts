import { create } from 'zustand';
import {
  getMyMcpPolicy,
  getSessionMcpPolicy,
  setMyMcpPolicy,
  setSessionMcpPolicy,
  type McpPolicyEntry,
  type McpVerdict,
} from '@/services/api';

// Cross-instance signal that the user's GLOBAL MCP tool policy changed, so a conversation-scope
// editor (which shows tools as INHERITED from the global defaults) can re-fetch and reflect it —
// e.g. after the stacked global Settings modal edits it above an open conversation panel.
interface McpPolicyVersionState {
  global: number;
  bumpGlobal: () => void;
  // Same signal for a CONVERSATION's per-tool policy (e.g. the in-bubble ask bar wrote
  // "deny for this conversation" while the conversation MCP popup is open).
  session: number;
  bumpSession: () => void;
}

export const useMcpPolicyVersion = create<McpPolicyVersionState>((set) => ({
  global: 0,
  bumpGlobal: () => set((s) => ({ global: s.global + 1 })),
  session: 0,
  bumpSession: () => set((s) => ({ session: s.session + 1 })),
}));

// Serialize ALL per-server full-state policy PUTs in this tab — the editor's optimistic
// slider saves AND the in-bubble ask bar's fetch→merge→PUT — through one chain, so
// out-of-order delivery / an interleaved read-modify-write can't regress the server to
// a stale snapshot (the endpoints replace a server's whole entry set). Cross-TAB writes
// can still race last-writer-wins; fixing that needs a server-side version/If-Match.
let policySaveChain: Promise<unknown> = Promise.resolve();
export function runPolicySave<T>(fn: () => Promise<T>): Promise<T> {
  const p = policySaveChain.then(fn, fn); // run in order, regardless of prior outcome
  policySaveChain = p.catch(() => {}); // a rejection must not break the chain
  return p;
}

// Test hook: a test that deliberately hangs a PUT must not wedge the chain for the
// rest of its file. Production never needs this — fetchAPI's timeouts settle every
// write eventually, so the chain always advances.
export function resetPolicySaveChainForTests(): void {
  policySaveChain = Promise.resolve();
}

// The policy PUTs are FULL-STATE per server: merge the new verdict into the server's
// current entries, else a one-tool write would wipe the others.
function mergePolicyEntry(
  policies: McpPolicyEntry[],
  serverKey: string,
  tool: string,
  verdict: McpVerdict,
): { tool: string; verdict: McpVerdict }[] {
  const entries = policies
    .filter((e) => e.server_key === serverKey && e.tool !== tool)
    .map((e) => ({ tool: e.tool, verdict: e.verdict }));
  entries.push({ tool, verdict });
  return entries;
}

// One-tool verdict write for the ask bar: fetch→merge→PUT through the shared chain
// (so it can't interleave with an editor's optimistic saves), then bump the matching
// policy version so any open editor re-fetches. Rejections propagate — the caller
// decides whether the write is best-effort.
export async function persistToolVerdict(
  target: { scope: 'global' } | { scope: 'session'; sessionId: string },
  serverKey: string,
  tool: string,
  verdict: McpVerdict,
): Promise<void> {
  if (target.scope === 'session') {
    await runPolicySave(async () => {
      const cur = await getSessionMcpPolicy(target.sessionId);
      await setSessionMcpPolicy(
        target.sessionId,
        serverKey,
        mergePolicyEntry(cur.policies, serverKey, tool, verdict),
      );
    });
    useMcpPolicyVersion.getState().bumpSession();
  } else {
    await runPolicySave(async () => {
      const cur = await getMyMcpPolicy();
      await setMyMcpPolicy(serverKey, mergePolicyEntry(cur.policies, serverKey, tool, verdict));
    });
    useMcpPolicyVersion.getState().bumpGlobal();
  }
}
