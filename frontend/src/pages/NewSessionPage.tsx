import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { Cluster } from '@/types/api';
import * as api from '@/services/api';

function parseHash(): { cluster?: string; q?: string } {
  const raw = window.location.hash.replace(/^#/, '');
  if (!raw) return {};
  const params = new URLSearchParams(raw);
  return {
    cluster: params.get('cluster') ?? undefined,
    q: params.get('q') ?? undefined,
  };
}

export function NewSessionPage() {
  const navigate = useNavigate();
  const { createSession, setPendingPrompt } = useSessionsStore();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hash, setHash] = useState(window.location.hash);

  useEffect(() => {
    const onHashChange = () => setHash(window.location.hash);
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const startSession = useCallback(async (cluster: string, q: string, signal: AbortSignal) => {
    setError(null);
    setLoading(true);

    try {
      const clusters = await api.getClusters();
      if (signal.aborted) return;

      const lc = cluster.toLowerCase();
      const match = clusters.find(
        (c: Cluster) =>
          c.name.toLowerCase() === lc ||
          c.display_name.toLowerCase() === lc ||
          c.id === cluster,
      );

      if (!match) {
        setError(`Cluster "${cluster}" not found`);
        setLoading(false);
        return;
      }

      if (!match.is_active) {
        setError(`Cluster "${match.display_name}" is not active`);
        setLoading(false);
        return;
      }

      setPendingPrompt(q);
      const session = await createSession(match.id);
      if (!signal.aborted) {
        window.history.replaceState(null, '', window.location.pathname);
        navigate(`/session/${session.id}`, { replace: true });
      }
    } catch (e) {
      if (!signal.aborted) {
        setError((e as Error).message);
        setLoading(false);
      }
    }
  }, [createSession, navigate, setPendingPrompt]);

  useEffect(() => {
    const { cluster, q } = parseHash();
    if (!cluster || !q) return;

    const controller = new AbortController();
    startSession(cluster, q, controller.signal);
    return () => controller.abort();
  }, [hash, startSession]);

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center space-y-4">
          <div className="text-red-400 text-lg">Error</div>
          <div className="text-gray-400">{error}</div>
          <button
            onClick={() => {
              window.history.replaceState(null, '', window.location.pathname);
              setHash('');
              setError(null);
            }}
            className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-md text-sm"
          >
            Go to Home
          </button>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-gray-400">Setting up...</div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex items-center justify-center">
      <p className="text-gray-500">Select a cluster to start debugging</p>
    </div>
  );
}
