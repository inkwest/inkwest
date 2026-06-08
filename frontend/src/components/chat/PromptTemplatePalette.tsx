import { useEffect, useMemo, useState } from 'react';
import { LibraryBig } from 'lucide-react';
import { Button, Modal } from '@/components/ui';
import { usePromptTemplatesStore } from '@/store/promptTemplates';
import { PromptTemplate } from '@/types/api';

interface Props {
  // Called with the chosen template's content. The caller decides what to do
  // with it (here: replace the chat input, leaving it editable before send).
  onSelect: (content: string) => void;
  disabled?: boolean;
}

export function PromptTemplatePalette({ onSelect, disabled }: Props) {
  const { templates, isLoading, error, fetchTemplates } = usePromptTemplatesStore();
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');

  // Refresh the list each time the palette opens so newly created templates show.
  useEffect(() => {
    if (isOpen) fetchTemplates();
  }, [isOpen, fetchTemplates]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return templates;
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        (t.description || '').toLowerCase().includes(q) ||
        t.content.toLowerCase().includes(q)
    );
  }, [templates, query]);

  const handleClose = () => {
    setIsOpen(false);
    setQuery('');
  };

  const handleSelect = (template: PromptTemplate) => {
    onSelect(template.content);
    handleClose();
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        disabled={disabled}
        title="Insert a prompt template"
        aria-label="Insert a prompt template"
        className="flex h-full items-center justify-center px-3 rounded-lg border border-gray-600 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <LibraryBig className="h-4 w-4" />
      </button>

      <Modal isOpen={isOpen} onClose={handleClose} title="Prompt templates" size="lg">
        <div className="flex flex-col" style={{ maxHeight: '70vh' }}>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
            placeholder="Search templates…"
            className="w-full bg-gray-900 text-gray-100 text-sm rounded-md px-3 py-2 mb-3 border border-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />

          <div className="overflow-y-auto -mx-2 px-2">
            {isLoading && <p className="text-gray-400 text-sm py-3">Loading templates…</p>}
            {error && <p className="text-red-400 text-sm py-3">{error}</p>}
            {!isLoading && !error && filtered.length === 0 && (
              <p className="text-gray-400 text-sm py-3">
                {query ? 'No templates match your search.' : 'No templates available.'}
              </p>
            )}
            <div className="space-y-2">
              {filtered.map((template) => (
                <button
                  key={template.id}
                  type="button"
                  onClick={() => handleSelect(template)}
                  className="w-full text-left p-3 rounded-md bg-gray-900/60 border border-gray-700 hover:border-blue-500 hover:bg-gray-900 transition-colors"
                >
                  <div className="text-sm font-medium text-gray-100">{template.name}</div>
                  {template.description && (
                    <div className="text-xs text-gray-400 mt-0.5">{template.description}</div>
                  )}
                  <div className="text-xs text-gray-500 mt-1 line-clamp-2 whitespace-pre-wrap">
                    {template.content}
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="flex justify-end pt-4">
            <Button variant="secondary" size="sm" onClick={handleClose}>
              Cancel
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
}
