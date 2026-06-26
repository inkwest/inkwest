import { useCallback, useEffect, useState } from 'react';
import { Power, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui';
import { getActiveSandboxes, terminateSandbox, type ActiveSandbox } from '@/services/adminApi';

// Live list of conversation sandboxes running in the Docker/dind daemon, with a per-row
// terminate action (a section of the Sandbox card). Self-contained (own fetch state). Ground
// truth from the daemon, so it also surfaces orphans the in-process registry forgot.
export function RunningSandboxesSection() {
  const [sandboxes, setSandboxes] = useState<ActiveSandbox[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Inline two-step confirm (no blocking window.confirm): row id armed for termination.
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [killingId, setKillingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { sandboxes } = await getActiveSandboxes();
      setSandboxes(sandboxes);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  const terminate = useCallback(
    async (sessionId: string) => {
      setKillingId(sessionId);
      setError(null);
      try {
        await terminateSandbox(sessionId);
        setConfirmId(null);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to terminate');
      } finally {
        setKillingId(null);
      }
    },
    [load]
  );

  useEffect(() => {
    void load();
  }, [load]);

  const count = sandboxes?.length ?? 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-200 flex items-center gap-2">
          Running
          <span className="font-normal text-gray-400">({count})</span>
        </h3>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void load()}
          disabled={loading || killingId !== null}
        >
          <RefreshCw className={loading ? 'w-4 h-4 animate-spin' : 'w-4 h-4'} />
        </Button>
      </div>
      {loading && sandboxes === null ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : error && sandboxes === null ? (
        <p className="text-sm text-red-400">{error}</p>
      ) : count === 0 ? (
        <p className="text-sm text-gray-500">No conversation sandboxes are currently running.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="py-2 pr-4 font-medium">Owner</th>
                <th className="py-2 pr-4 font-medium">Session</th>
                <th className="py-2 pr-4 font-medium">Uptime</th>
                <th className="py-2 pr-4 font-medium">State</th>
                <th className="py-2 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sandboxes!.map((s) => (
                <tr key={s.session_id} className="border-b border-gray-900">
                  <td className="py-2 pr-4 break-all">
                    {s.user_email ?? s.user_id ?? <span className="text-gray-500">—</span>}
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs break-all">{s.session_id}</td>
                  <td className="py-2 pr-4 text-gray-300">{s.status}</td>
                  <td className="py-2 pr-4">
                    {s.in_use > 0 ? (
                      <span className="text-green-400">active ({s.in_use})</span>
                    ) : (
                      <span className="text-gray-500">idle</span>
                    )}
                  </td>
                  <td className="py-2 text-right whitespace-nowrap">
                    {confirmId === s.session_id ? (
                      <span className="inline-flex gap-2 justify-end">
                        <Button
                          variant="danger"
                          size="sm"
                          isLoading={killingId === s.session_id}
                          disabled={killingId !== null}
                          onClick={() => void terminate(s.session_id)}
                        >
                          Confirm
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={killingId !== null}
                          onClick={() => setConfirmId(null)}
                        >
                          Cancel
                        </Button>
                      </span>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-red-400 hover:bg-red-950/40 hover:text-red-300"
                        disabled={killingId !== null}
                        onClick={() => setConfirmId(s.session_id)}
                        aria-label="Terminate"
                        title="Terminate"
                      >
                        <Power className="w-4 h-4" />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-gray-500 mt-3">
            Terminate kills the container pair (even mid-turn — that turn errors out) but keeps the
            conversation’s durable state, so it cold-starts and resumes on the owner’s next turn.
          </p>
          {error && <p className="text-xs text-red-400 mt-2">{error}</p>}
        </div>
      )}
    </div>
  );
}
