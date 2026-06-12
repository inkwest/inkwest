import { useEffect, useMemo, useState } from 'react';
import { LibraryBig } from 'lucide-react';
import { Button, Modal } from '@/components/ui';
import { useSkillsStore } from '@/store/skills';
import { Skill } from '@/types/api';
import { cn } from '@/lib/utils';

interface Props {
  // Called with the chosen template's slug. The caller turns it into a `/<slug>`
  // skill invocation in the chat input (see ChatInput), editable before send.
  onSelect: (slug: string) => void;
  disabled?: boolean;
  // Override the trigger button styling (e.g. a square corner button).
  className?: string;
}

export function SkillPalette({ onSelect, disabled, className }: Props) {
  const { templates, isLoading, error, fetchTemplates } = useSkillsStore();
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

  const handleSelect = (template: Skill) => {
    onSelect(template.slug);
    handleClose();
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        disabled={disabled}
        title="Insert a skill"
        aria-label="Insert a skill"
        className={cn(
          'flex items-center justify-center px-3 rounded-lg border border-gray-600 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed',
          className,
        )}
      >
        <LibraryBig className="h-4 w-4" />
      </button>

      <Modal isOpen={isOpen} onClose={handleClose} title="Skills" size="lg">
        <div className="flex flex-col" style={{ maxHeight: '70vh' }}>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
            placeholder="Search skills…"
            className="w-full bg-gray-900 text-gray-100 text-sm rounded-md px-3 py-2 mb-3 border border-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />

          <div className="overflow-y-auto -mx-2 px-2">
            {isLoading && <p className="text-gray-400 text-sm py-3">Loading skills…</p>}
            {error && <p className="text-red-400 text-sm py-3">{error}</p>}
            {!isLoading && !error && filtered.length === 0 && (
              <p className="text-gray-400 text-sm py-3">
                {query ? 'No skills match your search.' : 'No skills available.'}
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
