import { useEffect, useState } from 'react';
import { Plus, Edit2, Trash2 } from 'lucide-react';
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
import { useRoleMappingStore, useClusterStore } from '@/store/admin';
import { RoleMapping, CreateRoleMappingRequest } from '@/types/admin';

export function RoleMappingsPage() {
  const {
    roleMappings,
    isLoading,
    error,
    fetchRoleMappings,
    createRoleMapping,
    updateRoleMapping,
    deleteRoleMapping,
    clearError,
  } = useRoleMappingStore();

  const { clusters, fetchClusters } = useClusterStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingMapping, setEditingMapping] = useState<RoleMapping | null>(null);
  const [formData, setFormData] = useState<Partial<CreateRoleMappingRequest>>({
    role_name: '',
    can_web_search: true,
  });

  useEffect(() => {
    fetchRoleMappings();
    fetchClusters();
  }, [fetchRoleMappings, fetchClusters]);

  const handleOpenModal = (mapping?: RoleMapping) => {
    if (mapping) {
      setEditingMapping(mapping);
      setFormData({
        role_name: mapping.role_name,
        description: mapping.description || '',
        allowed_clusters: mapping.allowed_clusters,
        allowed_namespaces: mapping.allowed_namespaces,
        denied_namespaces: mapping.denied_namespaces,
        can_web_search: mapping.can_web_search,
        daily_token_budget: mapping.daily_token_budget || undefined,
      });
    } else {
      setEditingMapping(null);
      setFormData({
        role_name: '',
        allowed_clusters: [],
        can_web_search: true,
      });
    }
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingMapping(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingMapping) {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { role_name, ...updateData } = formData;
        await updateRoleMapping(editingMapping.id, updateData);
      } else {
        await createRoleMapping(formData as CreateRoleMappingRequest);
      }
      handleCloseModal();
    } catch (err) {
      // Error is handled by the store
    }
  };

  const handleDelete = async (id: string) => {
    if (confirm('Are you sure you want to delete this role mapping?')) {
      await deleteRoleMapping(id);
    }
  };

  const handleToggleActive = async (mapping: RoleMapping) => {
    await updateRoleMapping(mapping.id, { is_active: !mapping.is_active });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Role Mappings</h1>
          <p className="text-gray-400 mt-1">Configure OIDC role to cluster access mappings</p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add Role Mapping
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
                  <TableHead>Role Name</TableHead>
                  <TableHead>Permissions</TableHead>
                  <TableHead>Clusters</TableHead>
                  <TableHead>Namespaces</TableHead>
                  <TableHead>Token Budget</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {roleMappings.map((mapping) => (
                  <TableRow key={mapping.id}>
                    <TableCell>
                      <div>
                        <p className="font-medium">{mapping.role_name}</p>
                        {mapping.description && (
                          <p className="text-sm text-gray-400">{mapping.description}</p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {mapping.can_web_search && (
                          <Badge variant="info">web-search</Badge>
                        )}
                        {!mapping.can_web_search && (
                          <span className="text-gray-500">read-only</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="text-sm">
                        {mapping.allowed_clusters.length > 0 ? (
                          <span className="text-blue-400">
                            {mapping.allowed_clusters
                              .map((id) => clusters.find((c) => c.id === id)?.display_name || id)
                              .slice(0, 3)
                              .join(', ')}
                            {mapping.allowed_clusters.length > 3 && '...'}
                          </span>
                        ) : (
                          <span className="text-gray-500">All clusters</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="text-sm">
                        {mapping.allowed_namespaces.length > 0 ? (
                          <span className="text-green-400">
                            Allow: {mapping.allowed_namespaces.slice(0, 3).join(', ')}
                            {mapping.allowed_namespaces.length > 3 && '...'}
                          </span>
                        ) : mapping.denied_namespaces.length > 0 ? (
                          <span className="text-red-400">
                            Deny: {mapping.denied_namespaces.slice(0, 3).join(', ')}
                            {mapping.denied_namespaces.length > 3 && '...'}
                          </span>
                        ) : (
                          <span className="text-gray-500">All namespaces</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      {mapping.daily_token_budget
                        ? mapping.daily_token_budget.toLocaleString()
                        : '-'}
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleActive(mapping)}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          mapping.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {mapping.is_active ? 'Active' : 'Inactive'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleOpenModal(mapping)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(mapping.id)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {roleMappings.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8 text-gray-400">
                      No role mappings configured. Click "Add Role Mapping" to create one.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Role Mapping Form Modal */}
      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editingMapping ? 'Edit Role Mapping' : 'Create Role Mapping'}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label="Role Name"
            value={formData.role_name || ''}
            onChange={(e) => setFormData({ ...formData, role_name: e.target.value })}
            required
            disabled={!!editingMapping}
            helperText="Must match the role name from your OIDC provider"
          />

          <Input
            label="Description"
            value={formData.description || ''}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          />

          <div>
            <p className="text-sm font-medium text-gray-300 mb-2">Allowed Clusters</p>
            <div className="space-y-2">
              {clusters.map((cluster) => (
                <label
                  key={cluster.id}
                  className="flex items-center gap-3 p-2 rounded hover:bg-gray-700 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={formData.allowed_clusters?.includes(cluster.id) ?? false}
                    onChange={(e) => {
                      const current = formData.allowed_clusters ?? [];
                      setFormData({
                        ...formData,
                        allowed_clusters: e.target.checked
                          ? [...current, cluster.id]
                          : current.filter((id) => id !== cluster.id),
                      });
                    }}
                    className="w-4 h-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
                  />
                  <span>{cluster.display_name}</span>
                </label>
              ))}
              {clusters.length === 0 && (
                <p className="text-sm text-gray-500">No clusters configured</p>
              )}
            </div>
            <p className="text-sm text-gray-500 mt-1">
              Leave empty to allow all clusters.
            </p>
          </div>

          <Input
            label="Allowed Namespaces"
            value={formData.allowed_namespaces?.join(', ') || ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                allowed_namespaces: e.target.value
                  .split(',')
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            helperText="Comma-separated list. Leave empty to allow all."
          />

          <Input
            label="Denied Namespaces"
            value={formData.denied_namespaces?.join(', ') || ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                denied_namespaces: e.target.value
                  .split(',')
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            helperText="Comma-separated list. E.g., kube-system, kube-public"
          />

          <Input
            label="Daily Token Budget"
            type="number"
            value={formData.daily_token_budget || ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                daily_token_budget: e.target.value ? parseInt(e.target.value) : undefined,
              })
            }
            helperText="Leave empty for no limit"
          />

          <div>
            <p className="text-sm font-medium text-gray-300 mb-2">Permissions</p>
            <div className="space-y-2">
              <label className="flex items-center gap-3 p-2 rounded hover:bg-gray-700 cursor-pointer">
                <input
                  type="checkbox"
                  checked={formData.can_web_search ?? true}
                  onChange={(e) =>
                    setFormData({ ...formData, can_web_search: e.target.checked })
                  }
                  className="w-4 h-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
                />
                <span>Allow web search for documentation</span>
              </label>
            </div>
          </div>

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit">
              {editingMapping ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
