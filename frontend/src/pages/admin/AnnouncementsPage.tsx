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
  Select,
  ToggleSwitch,
} from '@/components/ui';
import { AnnouncementBar } from '@/components/layout/AnnouncementBanners';
import { LEVEL_BG } from '@/lib/announcementColors';
import { useAnnouncementAdminStore, useUserStore } from '@/store/admin';
import { ApiError } from '@/services/api';
import type { AnnouncementAdmin, AnnouncementWrite } from '@/types/admin';
import type { AnnouncementLevel } from '@/types/api';

// Mirrors the API bounds (schemas.ANNOUNCEMENT_*); the server stays the authority.
const MESSAGE_MAX = 500;
const OPACITY_MIN = 10;
const OPACITY_MAX = 100;

const LEVEL_OPTIONS: { value: AnnouncementLevel; label: string }[] = [
  { value: 'info', label: 'Info' },
  { value: 'success', label: 'Success' },
  { value: 'warning', label: 'Warning' },
  { value: 'danger', label: 'Danger' },
];

interface FormState {
  message: string;
  level: AnnouncementLevel;
  useLevelColor: boolean;
  // null = no custom color picked yet: the picker follows the level's color.
  color: string | null;
  autoText: boolean;
  textColor: string;
  opacity: number;
  dismissible: boolean;
  enabled: boolean;
  // datetime-local value ('YYYY-MM-DDTHH:mm', browser time); '' = no end date.
  endsAt: string;
}

const EMPTY_FORM: FormState = {
  message: '',
  level: 'info',
  useLevelColor: true,
  color: null,
  autoText: true,
  textColor: '#ffffff',
  opacity: OPACITY_MAX,
  dismissible: true,
  enabled: true,
  endsAt: '',
};

// Code points, like Python's len() on the server: an emoji counts once.
const messageLength = (message: string) => [...message.trim()].length;

const pad = (n: number) => String(n).padStart(2, '0');

// The input value has no zone: it is read as browser-local time, sent as an aware UTC instant.
function localInputToIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

function isoToLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const formOf = (a: AnnouncementAdmin): FormState => ({
  message: a.message,
  level: a.level,
  useLevelColor: a.color === null,
  color: a.color,
  autoText: a.text_color === null,
  textColor: a.text_color ?? '#ffffff',
  opacity: a.opacity,
  dismissible: a.dismissible,
  enabled: a.enabled,
  endsAt: isoToLocalInput(a.ends_at),
});

const pickedColor = (f: FormState) => f.color ?? LEVEL_BG[f.level];

// `editing` keeps its ends_at when the minute-precision input was left alone: its seconds would
// otherwise be cut, moving the expiry earlier.
const bodyOf = (f: FormState, editing: AnnouncementAdmin | null = null): AnnouncementWrite => ({
  message: f.message,
  level: f.level,
  color: f.useLevelColor ? null : pickedColor(f),
  text_color: f.autoText ? null : f.textColor,
  opacity: f.opacity,
  dismissible: f.dismissible,
  enabled: f.enabled,
  ends_at:
    editing && f.endsAt === isoToLocalInput(editing.ends_at) ? editing.ends_at : localInputToIso(f.endsAt),
});

// A PUT is a full replace: resend every field of the row.
const rowBody = (a: AnnouncementAdmin): AnnouncementWrite => ({
  message: a.message,
  level: a.level,
  color: a.color,
  text_color: a.text_color,
  opacity: a.opacity,
  dismissible: a.dismissible,
  enabled: a.enabled,
  ends_at: a.ends_at,
});

// fetchAPI JSON-encodes a non-string detail: a 422 carries pydantic's [{loc, msg}] list.
function errorMessages(e: unknown): string[] {
  const message = (e as Error).message;
  if (e instanceof ApiError && e.status === 422) {
    try {
      const detail: unknown = JSON.parse(message);
      if (Array.isArray(detail)) {
        return detail.map((d: { loc?: unknown; msg?: unknown }) => {
          const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
          return field ? `${field}: ${String(d.msg)}` : String(d.msg);
        });
      }
    } catch {
      // A plain-string detail.
    }
  }
  return [message];
}

// Accessible name per row: markdown syntax dropped, code points so an emoji is never split, and
// the creation time so two banners sharing a prefix still differ.
const rowLabel = (a: AnnouncementAdmin) => {
  const text = a.message
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_~`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return `${[...text].slice(0, 40).join('')} (created ${new Date(a.created_at).toLocaleString()})`;
};

const isExpired = (a: AnnouncementAdmin) => a.ends_at !== null && Date.parse(a.ends_at) <= Date.now();

export function AnnouncementsPage() {
  const {
    announcements,
    isLoading,
    error,
    fetchAnnouncements,
    createAnnouncement,
    updateAnnouncement,
    deleteAnnouncement,
    clearError,
  } = useAnnouncementAdminStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editing, setEditing] = useState<AnnouncementAdmin | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  // One write at a time, and no form opened over one: a late answer must not replay a stale row
  // or close a form it was not submitted from.
  const [pending, setPending] = useState(false);
  const users = useUserStore((s) => s.users);
  const fetchUsers = useUserStore((s) => s.fetchUsers);

  useEffect(() => {
    fetchAnnouncements();
    // Never throws: on failure the list stays empty and authors fall back to their short id.
    fetchUsers();
  }, [fetchAnnouncements, fetchUsers]);

  // Another admin may have edited since: refetch on return so a full-replace PUT starts fresh.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') void fetchAnnouncements();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [fetchAnnouncements]);

  const authorOf = (id: string | null) => {
    if (id === null) return 'deleted user';
    const user = users.find((u) => u.id === id);
    return user ? user.display_name || user.email : id.slice(0, 8);
  };

  const update = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));

  const handleOpenModal = (a?: AnnouncementAdmin) => {
    setEditing(a ?? null);
    setForm(a ? formOf(a) : EMPTY_FORM);
    setFormErrors([]);
    clearError();
    // A failed toggle/delete is old news once the admin moves on to the form.
    setActionError(null);
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditing(null);
    setFormErrors([]);
  };

  // Esc, X, backdrop and Cancel all land here: a write in flight cannot be cancelled, so the form
  // stays open to show its outcome.
  const requestClose = () => {
    if (!pending) handleCloseModal();
  };

  const length = messageLength(form.message);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pending) return;
    if (length === 0) return setFormErrors(['message: Message is required.']);
    if (length > MESSAGE_MAX) return setFormErrors([`message: At most ${MESSAGE_MAX} characters.`]);
    setPending(true);
    try {
      if (editing) await updateAnnouncement(editing.id, bodyOf(form, editing));
      else await createAnnouncement(bodyOf(form));
      handleCloseModal();
    } catch (err) {
      setFormErrors(errorMessages(err));
    } finally {
      setPending(false);
    }
  };

  const runAction = async (action: () => Promise<void>) => {
    if (pending) return;
    setPending(true);
    setActionError(null);
    try {
      await action();
    } catch (err) {
      setActionError(errorMessages(err).join(' '));
    } finally {
      setPending(false);
    }
  };

  const handleToggleEnabled = (a: AnnouncementAdmin) =>
    runAction(() => updateAnnouncement(a.id, { ...rowBody(a), enabled: !a.enabled }));

  const handleDelete = (a: AnnouncementAdmin) => {
    if (pending) return;
    if (confirm('Delete this announcement? This cannot be undone.')) {
      return runAction(() => deleteAnnouncement(a.id));
    }
  };

  // What the banner would look like with the current form values (004-AC7).
  // Trimmed like the server stores it: a leading indent would otherwise preview as a code block.
  const preview = {
    ...bodyOf({ ...form, message: form.message.trim(), endsAt: '' }),
    id: editing?.id ?? 'preview',
    updated_at: '',
  };

  const shownError = error ?? actionError;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Announcements</h1>
          <p className="text-gray-400 mt-1">
            Banners shown at the top of every page to every signed-in user
          </p>
        </div>
        <Button onClick={() => handleOpenModal()} disabled={pending}>
          <Plus className="w-4 h-4 mr-2" />
          Add Announcement
        </Button>
      </div>

      {shownError && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {shownError}
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
                  <TableHead>Banner</TableHead>
                  <TableHead>Level</TableHead>
                  <TableHead>Enabled</TableHead>
                  <TableHead>Close</TableHead>
                  <TableHead>Ends</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Author</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {announcements.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell className="min-w-[16rem]">
                      <AnnouncementBar announcement={a} decorative />
                    </TableCell>
                    <TableCell>
                      <Badge>{a.level}</Badge>
                    </TableCell>
                    <TableCell>
                      <ToggleSwitch
                        checked={a.enabled}
                        onChange={() => handleToggleEnabled(a)}
                        // aria-disabled, not disabled: disabling the focused control drops focus to <body>.
                        aria-disabled={pending}
                        className="aria-disabled:cursor-not-allowed aria-disabled:opacity-40"
                        aria-label={`Enabled: ${rowLabel(a)}`}
                      />
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {a.dismissible ? 'Dismissible' : 'Pinned'}
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {a.ends_at ? (
                        <span className="inline-flex items-center gap-2">
                          {new Date(a.ends_at).toLocaleString()}
                          {isExpired(a) && <Badge variant="danger">expired</Badge>}
                        </span>
                      ) : (
                        <span className="text-gray-500">never</span>
                      )}
                    </TableCell>
                    <TableCell className="text-gray-400">
                      {new Date(a.created_at).toLocaleDateString()}
                    </TableCell>
                    <TableCell className="text-gray-400">{authorOf(a.created_by)}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Edit announcement: ${rowLabel(a)}`}
                          disabled={pending}
                          onClick={() => handleOpenModal(a)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Delete announcement: ${rowLabel(a)}`}
                          aria-disabled={pending}
                          className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
                          onClick={() => handleDelete(a)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {announcements.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={8} className="text-center py-8 text-gray-400">
                      No announcements. Click "Add Announcement" to create one.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Modal
        isOpen={isModalOpen}
        onClose={requestClose}
        title={editing ? 'Edit Announcement' : 'Create Announcement'}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {formErrors.length > 0 && (
            <ul
              role="alert"
              className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded text-sm list-disc list-inside"
            >
              {formErrors.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          )}

          {/* Locked while a write runs: the form closes on success, so a later edit would be lost. */}
          <fieldset disabled={pending} className="space-y-4 border-0 p-0 m-0 min-w-0">
            <div>
              <label htmlFor="announcement-message" className="block text-sm font-medium text-gray-300 mb-1">
                Message
              </label>
              <textarea
                id="announcement-message"
                value={form.message}
                onChange={(e) => update({ message: e.target.value })}
                rows={3}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white placeholder-gray-500 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
              <p className="mt-1 text-sm text-gray-500 flex justify-between">
                <span>Inline markdown: **bold**, *italic*, `code`, [links](https://…)</span>
                <span data-testid="message-counter" className={length > MESSAGE_MAX ? 'text-red-400' : undefined}>
                  {length}/{MESSAGE_MAX}
                </span>
              </p>
            </div>

            <Select
              label="Level"
              value={form.level}
              options={LEVEL_OPTIONS}
              onChange={(e) => update({ level: e.target.value as AnnouncementLevel })}
            />

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1">
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input
                    type="checkbox"
                    checked={form.useLevelColor}
                    onChange={(e) => update({ useLevelColor: e.target.checked })}
                    className="rounded border-gray-600 bg-gray-800"
                  />
                  Use level color
                </label>
                <input
                  type="color"
                  aria-label="Background color"
                  value={pickedColor(form)}
                  disabled={form.useLevelColor}
                  onChange={(e) => update({ color: e.target.value })}
                  className="h-8 w-16 bg-gray-800 disabled:opacity-40"
                />
              </div>
              <div className="space-y-1">
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input
                    type="checkbox"
                    checked={form.autoText}
                    onChange={(e) => update({ autoText: e.target.checked })}
                    className="rounded border-gray-600 bg-gray-800"
                  />
                  Automatic text color
                </label>
                <input
                  type="color"
                  aria-label="Text color"
                  value={form.textColor}
                  disabled={form.autoText}
                  onChange={(e) => update({ textColor: e.target.value })}
                  className="h-8 w-16 bg-gray-800 disabled:opacity-40"
                />
              </div>
            </div>

            <div>
              <label htmlFor="announcement-opacity" className="block text-sm font-medium text-gray-300 mb-1">
                Opacity: {form.opacity}%
              </label>
              <input
                id="announcement-opacity"
                type="range"
                min={OPACITY_MIN}
                max={OPACITY_MAX}
                value={form.opacity}
                onChange={(e) => update({ opacity: Number(e.target.value) })}
                className="w-full"
              />
            </div>

            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={form.dismissible}
                onChange={(e) => update({ dismissible: e.target.checked })}
                className="rounded border-gray-600 bg-gray-800"
              />
              Users can close it
              <span className="text-gray-500">(unchecked = pinned, no close button)</span>
            </label>

            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => update({ enabled: e.target.checked })}
                className="rounded border-gray-600 bg-gray-800"
              />
              Enabled
            </label>

            <div>
              <label htmlFor="announcement-ends-at" className="block text-sm font-medium text-gray-300 mb-1">
                Ends at (optional, your local time)
              </label>
              <input
                id="announcement-ends-at"
                type="datetime-local"
                value={form.endsAt}
                onChange={(e) => update({ endsAt: e.target.value })}
                className="px-3 py-2 bg-gray-800 border border-gray-600 rounded-md text-white text-sm"
              />
            </div>

            <div>
              <p className="block text-sm font-medium text-gray-300 mb-1">Preview</p>
              {/* A visual copy of the textarea: inert keeps its links out of the tab order; aria-hidden
                  covers browsers without inert. The spread passes the attribute React 18 does not type. */}
              <div
                {...{ inert: '' }}
                aria-hidden="true"
                data-testid="announcement-preview"
                className="rounded overflow-hidden bg-gray-900"
              >
                <AnnouncementBar announcement={preview} decorative />
              </div>
            </div>
          </fieldset>

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={requestClose} disabled={pending}>
              Cancel
            </Button>
            <Button
              type="submit"
              aria-disabled={pending}
              className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
            >
              {editing ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
