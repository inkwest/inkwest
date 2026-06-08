import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useClustersStore } from '@/store/clusters';
import { useSessionsStore } from '@/store/sessions';
import { useAuthStore, useSystemPromptStore } from '@/store/admin';
import { Modal } from '@/components/ui/Modal';

interface Props {
  open: boolean;
  onClose: () => void;
}

const CUSTOM = '__custom__';

export function NewSessionDialog({ open, onClose }: Props) {
  const navigate = useNavigate();
  const { clusters, fetchClusters, isLoading, error } = useClustersStore();
  const { createSession } = useSessionsStore();
  const { user } = useAuthStore();
  const { prompts, fetchPrompts } = useSystemPromptStore();
  const isAdmin = !!user?.is_admin;

  const [selectedCluster, setSelectedCluster] = useState('');
  // '' = default prompt, CUSTOM = free-text, otherwise a system prompt id.
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [customPrompt, setCustomPrompt] = useState('');

  useEffect(() => {
    if (open) {
      fetchClusters();
      if (isAdmin) fetchPrompts(true); // active only
    }
  }, [open, isAdmin, fetchClusters, fetchPrompts]);

  // Preselect the default prompt once the list loads (don't clobber a manual
  // pick). The default is always present — it can't be deactivated.
  useEffect(() => {
    if (!isAdmin) return;
    const def = prompts.find((p) => p.is_default);
    if (def) setSelectedPrompt((cur) => (cur === '' ? def.id : cur));
  }, [isAdmin, prompts]);

  const clusterList = Array.isArray(clusters) ? clusters : [];
  const noClusters = !isLoading && !error && clusterList.length === 0;
  const customInvalid = selectedPrompt === CUSTOM && !customPrompt.trim();

  const handleCreate = async () => {
    // With no cluster configured, start a session without a Kube context.
    if (!noClusters && !selectedCluster) return;
    if (customInvalid) return;

    const opts =
      isAdmin && selectedPrompt === CUSTOM
        ? { systemPromptCustom: customPrompt.trim() }
        : isAdmin && selectedPrompt
          ? { systemPromptId: selectedPrompt }
          : undefined;

    const session = await createSession(selectedCluster || undefined, opts);
    onClose();
    navigate(`/session/${session.id}`);
  };

  return (
    <Modal isOpen={open} onClose={onClose} title="Start New Session" size="sm">
      <div className="mb-4">
        <label className="block text-sm text-gray-400 mb-2">
          Select Cluster
        </label>
        {isLoading ? (
          <div className="text-gray-400 text-sm">Loading clusters...</div>
        ) : error ? (
          <div className="text-red-400 text-sm">Failed to load clusters</div>
        ) : clusterList.length === 0 ? (
          <div className="text-gray-500 text-sm">
            No cluster configured — the session will start without a Kubernetes
            context. You can still use other tools (e.g. AWS, GitLab).
          </div>
        ) : (
          <select
            autoFocus
            value={selectedCluster}
            onChange={(e) => setSelectedCluster(e.target.value)}
            className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white"
          >
            <option value="">Choose a cluster...</option>
            {clusterList.map((cluster) => (
              <option key={cluster.id} value={cluster.id}>
                {cluster.display_name}
              </option>
            ))}
          </select>
        )}
      </div>

      {/* System prompt selection is admin-only; non-admins always get the default. */}
      {isAdmin && (
        <div className="mb-4">
          <label className="block text-sm text-gray-400 mb-2">
            System Prompt
          </label>
          <select
            value={selectedPrompt}
            onChange={(e) => setSelectedPrompt(e.target.value)}
            className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white"
          >
            {prompts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.is_default ? ' (default)' : ''}
              </option>
            ))}
            <option value={CUSTOM}>Custom…</option>
          </select>
          {selectedPrompt === CUSTOM && (
            <textarea
              value={customPrompt}
              onChange={(e) => setCustomPrompt(e.target.value)}
              rows={6}
              placeholder="Enter a custom system prompt for this session…"
              className="mt-2 w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white font-mono text-sm"
            />
          )}
        </div>
      )}

      <div className="flex gap-2 justify-end">
        <button
          onClick={onClose}
          className="px-4 py-2 text-gray-400 hover:text-white"
        >
          Cancel
        </button>
        <button
          onClick={handleCreate}
          disabled={(!noClusters && !selectedCluster) || customInvalid}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded text-white disabled:opacity-50"
        >
          Start
        </button>
      </div>
    </Modal>
  );
}
