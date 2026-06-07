import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useClustersStore } from '@/store/clusters';
import { useSessionsStore } from '@/store/sessions';
import { Modal } from '@/components/ui/Modal';

interface Props {
  open: boolean;
  onClose: () => void;
}

export function NewSessionDialog({ open, onClose }: Props) {
  const navigate = useNavigate();
  const { clusters, fetchClusters, isLoading, error } = useClustersStore();
  const { createSession } = useSessionsStore();
  const [selectedCluster, setSelectedCluster] = useState('');

  useEffect(() => {
    if (open) {
      fetchClusters();
    }
  }, [open, fetchClusters]);

  const clusterList = Array.isArray(clusters) ? clusters : [];
  const noClusters = !isLoading && !error && clusterList.length === 0;

  const handleCreate = async () => {
    // With no cluster configured, start a session without a Kube context.
    if (!noClusters && !selectedCluster) return;
    const session = await createSession(selectedCluster || undefined);
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

      <div className="flex gap-2 justify-end">
        <button
          onClick={onClose}
          className="px-4 py-2 text-gray-400 hover:text-white"
        >
          Cancel
        </button>
        <button
          onClick={handleCreate}
          disabled={!noClusters && !selectedCluster}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded text-white disabled:opacity-50"
        >
          Start
        </button>
      </div>
    </Modal>
  );
}
