import { useEffect, useState } from 'react';
import { Plus, Edit2, Trash2, Server } from 'lucide-react';
import {
  Button,
  Card,
  CardContent,
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  Badge,
  Modal,
  Input,
} from '@/components/ui';
import { useClusterStore } from '@/store/admin';
import { AdminCluster, CreateClusterRequest } from '@/services/adminApi';

export function ClustersPage() {
  const {
    clusters,
    isLoading,
    error,
    fetchClusters,
    createCluster,
    updateCluster,
    deleteCluster,
    clearError,
  } = useClusterStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingCluster, setEditingCluster] = useState<AdminCluster | null>(null);
  const [formData, setFormData] = useState<Partial<CreateClusterRequest>>({
    name: '',
    display_name: '',
    kubeconfig_path: '',
    context_name: '',
    description: '',
  });

  useEffect(() => {
    fetchClusters(false); // Get all clusters, including inactive
  }, [fetchClusters]);

  const handleOpenModal = (cluster?: AdminCluster) => {
    if (cluster) {
      setEditingCluster(cluster);
      setFormData({
        name: cluster.name,
        display_name: cluster.display_name,
        kubeconfig_path: cluster.kubeconfig_path,
        context_name: cluster.context_name || '',
        description: cluster.description || '',
      });
    } else {
      setEditingCluster(null);
      setFormData({
        name: '',
        display_name: '',
        kubeconfig_path: '',
        context_name: '',
        description: '',
      });
    }
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingCluster(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingCluster) {
        await updateCluster(editingCluster.id, {
          display_name: formData.display_name,
          kubeconfig_path: formData.kubeconfig_path,
          context_name: formData.context_name || undefined,
          description: formData.description || undefined,
        });
      } else {
        await createCluster(formData as CreateClusterRequest);
      }
      handleCloseModal();
    } catch {
      // Error is handled by the store
    }
  };

  const handleDelete = async (id: string) => {
    if (confirm('Are you sure you want to deactivate this cluster?')) {
      await deleteCluster(id);
    }
  };

  const handleToggleActive = async (cluster: AdminCluster) => {
    await updateCluster(cluster.id, { is_active: !cluster.is_active });
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Clusters</h1>
          <p className="text-gray-400 mt-1">Manage Kubernetes cluster configurations</p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add Cluster
        </Button>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {error}
        </div>
      )}

      <Card variant="bordered">
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Display Name</TableHead>
                  <TableHead>Kubeconfig Path</TableHead>
                  <TableHead>Context</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {clusters.map((cluster) => (
                  <TableRow key={cluster.id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <Server className="w-4 h-4 text-gray-400" />
                        {cluster.name}
                      </div>
                    </TableCell>
                    <TableCell>{cluster.display_name}</TableCell>
                    <TableCell>
                      <code className="text-xs bg-gray-700 px-2 py-1 rounded">
                        {cluster.kubeconfig_path}
                      </code>
                    </TableCell>
                    <TableCell>
                      {cluster.context_name ? (
                        <Badge variant="default">{cluster.context_name}</Badge>
                      ) : (
                        <span className="text-gray-500">default</span>
                      )}
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {formatDate(cluster.created_at)}
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleActive(cluster)}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          cluster.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {cluster.is_active ? 'Active' : 'Inactive'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleOpenModal(cluster)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(cluster.id)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {clusters.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8 text-gray-400">
                      No clusters configured. Click "Add Cluster" to create one.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Cluster Form Modal */}
      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editingCluster ? 'Edit Cluster' : 'Create Cluster'}
        size="md"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded text-sm">
              {error}
            </div>
          )}

          <Input
            label="Name"
            value={formData.name || ''}
            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            required
            disabled={!!editingCluster}
            helperText={editingCluster ? 'Name cannot be changed' : 'Unique identifier for the cluster'}
          />

          <Input
            label="Display Name"
            value={formData.display_name || ''}
            onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
            required
            helperText="Human-readable name shown in the UI"
          />

          <Input
            label="Kubeconfig Path"
            value={formData.kubeconfig_path || ''}
            onChange={(e) => setFormData({ ...formData, kubeconfig_path: e.target.value })}
            required
            helperText="Path to the kubeconfig file on the server (e.g., /home/user/.kube/config)"
          />

          <Input
            label="Context Name"
            value={formData.context_name || ''}
            onChange={(e) => setFormData({ ...formData, context_name: e.target.value })}
            helperText="Optional: specific context to use from kubeconfig (defaults to current-context)"
          />

          <Input
            label="Description"
            value={formData.description || ''}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
            helperText="Optional: describe the cluster purpose or environment"
          />

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit">
              {editingCluster ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
