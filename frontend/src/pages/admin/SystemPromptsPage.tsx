import { useEffect, useMemo, useState } from 'react';
import { Plus, Edit2, Trash2, Terminal, Star } from 'lucide-react';
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
import { useSystemPromptStore } from '@/store/admin';
import {
  AdminSystemPrompt,
  CreateSystemPromptRequest,
} from '@/services/adminApi';

export function SystemPromptsPage() {
  const {
    prompts,
    isLoading,
    error,
    fetchPrompts,
    createPrompt,
    updatePrompt,
    deletePrompt,
    clearError,
  } = useSystemPromptStore();

  const [query, setQuery] = useState('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingPrompt, setEditingPrompt] = useState<AdminSystemPrompt | null>(null);
  const [formData, setFormData] = useState<Partial<CreateSystemPromptRequest>>({
    name: '',
    content: '',
    description: '',
    is_default: false,
  });

  useEffect(() => {
    fetchPrompts(false); // Get all prompts, including inactive
  }, [fetchPrompts]);

  // Client-side search over name / description / content, mirroring the prompt
  // templates screen.
  const filteredPrompts = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return prompts;
    return prompts.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q) ||
        p.content.toLowerCase().includes(q)
    );
  }, [prompts, query]);

  const handleOpenModal = (prompt?: AdminSystemPrompt) => {
    if (prompt) {
      setEditingPrompt(prompt);
      setFormData({
        name: prompt.name,
        content: prompt.content,
        description: prompt.description || '',
        is_default: prompt.is_default,
      });
    } else {
      setEditingPrompt(null);
      setFormData({ name: '', content: '', description: '', is_default: false });
    }
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingPrompt(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingPrompt) {
        await updatePrompt(editingPrompt.id, {
          name: formData.name,
          content: formData.content,
          description: formData.description || undefined,
          // Only promote — demotion happens by promoting another prompt.
          ...(formData.is_default ? { is_default: true } : {}),
        });
      } else {
        await createPrompt(formData as CreateSystemPromptRequest);
      }
      handleCloseModal();
    } catch {
      // Error is handled by the store
    }
  };

  const handleDelete = async (prompt: AdminSystemPrompt) => {
    if (prompt.is_default) return; // guarded server-side too
    if (confirm('Are you sure you want to permanently delete this system prompt?')) {
      await deletePrompt(prompt.id);
    }
  };

  const handleSetDefault = async (prompt: AdminSystemPrompt) => {
    if (prompt.is_default) return;
    await updatePrompt(prompt.id, { is_default: true });
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">System Prompts</h1>
          <p className="text-gray-400 mt-1">
            Manage the system prompts loaded before any interaction. One is the
            default applied to non-admins; admins pick one (or custom) per session.
          </p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add System Prompt
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
                  <TableHead>Default</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredPrompts.map((prompt) => (
                  <TableRow key={prompt.id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <Terminal className="w-4 h-4 text-gray-400" />
                        {prompt.name}
                      </div>
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {prompt.description || <span className="text-gray-500">—</span>}
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleSetDefault(prompt)}
                        disabled={prompt.is_default}
                        title={prompt.is_default ? 'Current default' : 'Set as default'}
                        className={`text-xs px-2 py-1 rounded inline-flex items-center gap-1 ${
                          prompt.is_default
                            ? 'bg-yellow-900/50 text-yellow-400'
                            : 'text-gray-500 hover:text-yellow-400 cursor-pointer'
                        }`}
                      >
                        <Star
                          className="w-3 h-3"
                          fill={prompt.is_default ? 'currentColor' : 'none'}
                        />
                        {prompt.is_default ? 'Default' : 'Set default'}
                      </button>
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {formatDate(prompt.created_at)}
                    </TableCell>
                    <TableCell>
                      <span
                        className={`text-xs px-2 py-1 rounded ${
                          prompt.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {prompt.is_active ? 'Active' : 'Inactive'}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleOpenModal(prompt)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(prompt)}
                          disabled={prompt.is_default}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {filteredPrompts.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center py-8 text-gray-400">
                      {query
                        ? 'No system prompts match your search.'
                        : 'No system prompts configured. Click "Add System Prompt" to create one.'}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* System Prompt Form Modal */}
      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editingPrompt ? 'Edit System Prompt' : 'Create System Prompt'}
        size="md"
        className="!max-w-[80vw] w-[80vw] h-[80vh]"
      >
        <form onSubmit={handleSubmit} className="flex flex-col h-full gap-4">
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
            helperText="Unique name shown when picking a prompt for a session"
          />

          <div className="w-full flex-1 min-h-0 flex flex-col">
            <label htmlFor="prompt-content" className="block text-sm font-medium text-gray-300 mb-1">
              Content
            </label>
            <textarea
              id="prompt-content"
              value={formData.content || ''}
              onChange={(e) => setFormData({ ...formData, content: e.target.value })}
              required
              className="w-full flex-1 min-h-0 resize-none px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white placeholder-gray-500 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <p className="mt-1 text-sm text-gray-500">
              Blanks <code>{'{cluster_name}'}</code> and <code>{'{namespaces}'}</code>{' '}
              are filled in at run time with the session context.
            </p>
          </div>

          <Input
            label="Description"
            value={formData.description || ''}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
            helperText="Optional: describe what this system prompt is for"
          />

          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input
              type="checkbox"
              checked={!!formData.is_default}
              disabled={!!editingPrompt?.is_default}
              onChange={(e) => setFormData({ ...formData, is_default: e.target.checked })}
              className="rounded border-gray-600 bg-gray-800"
            />
            Default for non-admins
            {editingPrompt?.is_default && (
              <span className="text-gray-500">(promote another prompt to change)</span>
            )}
          </label>

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit">
              {editingPrompt ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
