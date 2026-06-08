import { useEffect, useMemo, useState } from 'react';
import { Plus, Edit2, Trash2, FileText } from 'lucide-react';
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
  Modal,
  Input,
} from '@/components/ui';
import { usePromptTemplateStore } from '@/store/admin';
import {
  AdminPromptTemplate,
  CreatePromptTemplateRequest,
} from '@/services/adminApi';

export function PromptTemplatesPage() {
  const {
    templates,
    isLoading,
    error,
    fetchTemplates,
    createTemplate,
    updateTemplate,
    deleteTemplate,
    clearError,
  } = usePromptTemplateStore();

  const [query, setQuery] = useState('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<AdminPromptTemplate | null>(null);
  const [formData, setFormData] = useState<Partial<CreatePromptTemplateRequest>>({
    name: '',
    content: '',
    description: '',
  });

  useEffect(() => {
    fetchTemplates(false); // Get all templates, including inactive
  }, [fetchTemplates]);

  // Client-side search over name / description / content, mirroring the AWS
  // role-selection filter (AwsAccountRoleModal).
  const filteredTemplates = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return templates;
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        (t.description || '').toLowerCase().includes(q) ||
        t.content.toLowerCase().includes(q)
    );
  }, [templates, query]);

  const handleOpenModal = (template?: AdminPromptTemplate) => {
    if (template) {
      setEditingTemplate(template);
      setFormData({
        name: template.name,
        content: template.content,
        description: template.description || '',
      });
    } else {
      setEditingTemplate(null);
      setFormData({ name: '', content: '', description: '' });
    }
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingTemplate(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingTemplate) {
        await updateTemplate(editingTemplate.id, {
          name: formData.name,
          content: formData.content,
          description: formData.description || undefined,
        });
      } else {
        await createTemplate(formData as CreatePromptTemplateRequest);
      }
      handleCloseModal();
    } catch {
      // Error is handled by the store
    }
  };

  const handleDelete = async (id: string) => {
    if (confirm('Are you sure you want to deactivate this template?')) {
      await deleteTemplate(id);
    }
  };

  const handleToggleActive = async (template: AdminPromptTemplate) => {
    await updateTemplate(template.id, { is_active: !template.is_active });
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Prompt Templates</h1>
          <p className="text-gray-400 mt-1">
            Manage reusable prompt templates offered as an editable palette in the chat
          </p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add Template
        </Button>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {error}
        </div>
      )}

      <Input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search by name, description or content…"
      />

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
                  <TableHead>Description</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredTemplates.map((template) => (
                  <TableRow key={template.id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <FileText className="w-4 h-4 text-gray-400" />
                        {template.name}
                      </div>
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {template.description || <span className="text-gray-500">—</span>}
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {formatDate(template.created_at)}
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleActive(template)}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          template.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {template.is_active ? 'Active' : 'Inactive'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleOpenModal(template)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(template.id)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {filteredTemplates.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8 text-gray-400">
                      {query
                        ? 'No templates match your search.'
                        : 'No templates configured. Click "Add Template" to create one.'}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Template Form Modal */}
      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editingTemplate ? 'Edit Template' : 'Create Template'}
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
            helperText="Unique name shown in the palette"
          />

          <div className="w-full">
            <label htmlFor="template-content" className="block text-sm font-medium text-gray-300 mb-1">
              Content
            </label>
            <textarea
              id="template-content"
              value={formData.content || ''}
              onChange={(e) => setFormData({ ...formData, content: e.target.value })}
              required
              rows={6}
              className="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white placeholder-gray-500 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <p className="mt-1 text-sm text-gray-500">
              Use blanks like __________ for parts the user fills in before sending
            </p>
          </div>

          <Input
            label="Description"
            value={formData.description || ''}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
            helperText="Optional: describe what this template is for"
          />

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit">
              {editingTemplate ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
