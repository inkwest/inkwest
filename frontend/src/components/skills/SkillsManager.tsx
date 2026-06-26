import { useMemo, useState } from 'react';
import { Plus, Edit2, Trash2, FileText, GitFork } from 'lucide-react';
import {
  Badge,
  Button,
  Input,
  Modal,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';

/** The minimal skill shape this component renders — admin Skill and personal UserSkill both fit. */
export interface ManagedSkill {
  id: string;
  name: string;
  description?: string | null;
  content: string;
  is_active: boolean;
  is_override?: boolean;
  created_at?: string;
}

export interface OverridableAdminSkill {
  name: string;
  alreadyOverridden: boolean;
}

export interface SkillsManagerProps {
  skills: ManagedSkill[];
  isLoading?: boolean;
  error?: string | null;
  onCreate: (content: string) => Promise<void>;
  onUpdate: (id: string, data: { content?: string; is_active?: boolean }) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  clearError?: () => void;
  /** Show the "override" badge column (personal skills). */
  showOverrideBadge?: boolean;
  /** Personal-only: admin skills the user may fork into an editable personal copy. */
  overridableAdmin?: OverridableAdminSkill[];
  onOverride?: (adminName: string) => Promise<void>;
  emptyHint?: string;
}

const PLACEHOLDER =
  '---\nname: my-skill\ndescription: When to use it (also drives auto-invoke)\narguments: [arg1, arg2]\n---\nInstructions… use $ARGUMENTS (or $1, $2) for the typed arguments.';

export function SkillsManager({
  skills,
  isLoading = false,
  error = null,
  onCreate,
  onUpdate,
  onDelete,
  clearError,
  showOverrideBadge = false,
  overridableAdmin,
  onOverride,
  emptyHint = 'No skills yet.',
}: SkillsManagerProps) {
  const [query, setQuery] = useState('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editing, setEditing] = useState<ManagedSkill | null>(null);
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q) ||
        s.content.toLowerCase().includes(q)
    );
  }, [skills, query]);

  const openModal = (skill?: ManagedSkill) => {
    setEditing(skill ?? null);
    setContent(skill?.content ?? '');
    setIsModalOpen(true);
  };

  const closeModal = () => {
    setIsModalOpen(false);
    setEditing(null);
    clearError?.();
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (editing) await onUpdate(editing.id, { content });
      else await onCreate(content);
      closeModal();
    } catch {
      // surfaced via the `error` prop
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    if (confirm('Permanently delete this personal skill?')) await onDelete(id);
  };

  const overridable = (overridableAdmin ?? []).filter((a) => !a.alreadyOverridden);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by name, description or content…"
          className="flex-1"
        />
        <Button onClick={() => openModal()} className="shrink-0 whitespace-nowrap">
          <Plus className="w-4 h-4 mr-2" />
          Add Skill
        </Button>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded text-sm">
          {error}
        </div>
      )}

      {onOverride && overridable.length > 0 && (
        <div className="rounded border border-gray-700 bg-gray-900/40 p-3 space-y-2">
          <p className="text-xs text-gray-400">
            Override a global skill — forks an editable copy that shadows the global one:
          </p>
          <div className="flex flex-wrap gap-2">
            {overridable.map((a) => (
              <button
                key={a.name}
                onClick={() => void onOverride(a.name)}
                className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-200"
              >
                <GitFork className="w-3 h-3" />
                {a.name}
              </button>
            ))}
          </div>
        </div>
      )}

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
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((skill) => (
              <TableRow key={skill.id}>
                <TableCell className="font-medium">
                  <div className="flex items-center gap-2">
                    <FileText className="w-4 h-4 text-gray-400 shrink-0" />
                    <span>{skill.name}</span>
                    {showOverrideBadge && skill.is_override && (
                      <Badge variant="warning" title="Shadows a global skill of the same name">
                        override
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-gray-400">
                  {skill.description || <span className="text-gray-500">—</span>}
                </TableCell>
                <TableCell>
                  <button
                    onClick={() => void onUpdate(skill.id, { is_active: !skill.is_active })}
                    className={`text-xs px-2 py-1 rounded cursor-pointer ${
                      skill.is_active
                        ? 'bg-green-900/50 text-green-400'
                        : 'bg-red-900/50 text-red-400'
                    }`}
                  >
                    {skill.is_active ? 'Active' : 'Inactive'}
                  </button>
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex items-center justify-end gap-2">
                    <Button variant="ghost" size="sm" onClick={() => openModal(skill)}>
                      <Edit2 className="w-4 h-4" />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => void remove(skill.id)}>
                      <Trash2 className="w-4 h-4 text-red-400" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center py-8 text-gray-400">
                  {query ? 'No skills match your search.' : emptyHint}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}

      <Modal
        isOpen={isModalOpen}
        onClose={closeModal}
        title={editing ? 'Edit Skill' : 'Create Skill'}
        size="xl"
      >
        <form onSubmit={submit} className="space-y-4">
          {error && (
            <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded text-sm">
              {error}
            </div>
          )}
          <div className="w-full">
            <label htmlFor="skill-content" className="block text-sm font-medium text-gray-300 mb-1">
              SKILL.md
            </label>
            <textarea
              id="skill-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              required
              rows={20}
              placeholder={PLACEHOLDER}
              className="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white placeholder-gray-500 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <p className="mt-1 text-sm text-gray-500">
              Full SKILL.md. The <code>---</code> frontmatter sets <code>name</code> (→ the
              /slug trigger), <code>description</code>, and optional <code>arguments</code>. Use{' '}
              <code>$ARGUMENTS</code> (or <code>$1</code>, <code>$2</code>) in the body for the
              typed arguments.
            </p>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" variant="secondary" onClick={closeModal}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {editing ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
