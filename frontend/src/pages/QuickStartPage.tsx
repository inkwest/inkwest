import { useEffect, useState } from 'react';
import { Link, useSearchParams, useNavigate } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { Cluster } from '@/types/api';
import * as api from '@/services/api';

export function QuickStartPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { createSession, setPendingPrompt } = useSessionsStore();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const clusterParam = searchParams.get('cluster');
  const promptParam = searchParams.get('prompt');

  useEffect(() => {
    if (!clusterParam) {
      setError('Missing "cluster" parameter');
      setLoading(false);
      return;
    }

    let cancelled = false;

    async function start() {
      try {
        const clusterList = await api.getClusters();
        if (cancelled) return;

        const match = clusterList.find((c: Cluster) =>
          c.name.toLowerCase() === clusterParam!.toLowerCase() ||
          c.display_name.toLowerCase() === clusterParam!.toLowerCase() ||
          c.id === clusterParam
        );

        if (!match) {
          setError(`Cluster "${clusterParam}" not found`);
          setLoading(false);
          return;
        }

        if (!match.is_active) {
          setError(`Cluster "${match.display_name}" is not active`);
          setLoading(false);
          return;
        }

        if (promptParam) {
          setPendingPrompt(promptParam);
        }

        const session = await createSession(match.id);
        if (!cancelled) {
          navigate(`/session/${session.id}`, { replace: true });
        }
      } catch (e) {
        if (!cancelled) {
          setError((e as Error).message);
          setLoading(false);
        }
      }
    }

    start();
    return () => { cancelled = true; };
  }, [clusterParam, promptParam, createSession, navigate, setPendingPrompt]);

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center space-y-4">
          <div className="text-red-400 text-lg">Quick Start Error</div>
          <div className="text-gray-400">{error}</div>
          <Link
            to="/"
            className="inline-block px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-md text-sm"
          >
            Go to Home
          </Link>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-gray-400">Setting up session...</div>
      </div>
    );
  }

  return null;
}
