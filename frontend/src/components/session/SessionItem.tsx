import { useState, useRef, useEffect } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { Trash2, Pencil, Check, X } from 'lucide-react';
import { Session } from '@/types/api';
import { formatRelativeTime, formatTokenCount, cn } from '@/lib/utils';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { ConfirmDialog } from '@/components/ui';

interface Props {
  session: Session;
}

export function SessionItem({ session }: Props) {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const isActive = sessionId === session.id;
  const { deleteSession, renameSession } = useSessionsStore();
  const setSidebarOpen = useSettingsStore((s) => s.setSidebarOpen);

  const [isHovered, setIsHovered] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(session.title || '');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  const handleDelete = async () => {
    const wasActive = isActive;
    await deleteSession(session.id);
    setShowDeleteConfirm(false);
    if (wasActive) {
      navigate('/');
    }
  };

  const handleStartRename = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setEditTitle(session.title || '');
    setIsEditing(true);
  };

  const handleSaveRename = async () => {
    if (editTitle.trim() && editTitle !== session.title) {
      await renameSession(session.id, editTitle.trim());
    }
    setIsEditing(false);
  };

  const handleCancelRename = () => {
    setEditTitle(session.title || '');
    setIsEditing(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSaveRename();
    } else if (e.key === 'Escape') {
      handleCancelRename();
    }
  };

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setShowDeleteConfirm(true);
  };

  if (isEditing) {
    return (
      <div className="px-3 py-2 rounded-lg bg-gray-700">
        <div className="flex items-center gap-2">
          <input
            ref={inputRef}
            type="text"
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={handleSaveRename}
            className="flex-1 bg-gray-600 text-white text-sm px-2 py-1 rounded border border-gray-500 focus:border-blue-500 focus:outline-none"
            placeholder="Session title"
          />
          <button
            onClick={handleSaveRename}
            className="p-1 text-green-400 hover:text-green-300"
          >
            <Check className="w-4 h-4" />
          </button>
          <button
            onClick={handleCancelRename}
            className="p-1 text-gray-400 hover:text-gray-300"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
    );
  }

  return (
    <>
      <Link
        to={`/session/${session.id}`}
        className={cn(
          'group block px-3 py-2 rounded-lg text-sm transition-colors relative',
          isActive
            ? 'bg-gray-700 text-white'
            : 'text-gray-300 hover:bg-gray-700/50'
        )}
        onClick={() => setSidebarOpen(false)}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
      >
        <div className="flex items-center justify-between">
          <div className="flex-1 min-w-0 pr-2">
            <div className="font-medium truncate" title={session.title || 'New conversation'}>
              {session.title || 'New conversation'}
            </div>
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              {session.cluster && (
                <>
                  <span className="truncate max-w-[100px]">{session.cluster.display_name}</span>
                  <span className="text-gray-600">&middot;</span>
                </>
              )}
              <span className="shrink-0">{formatRelativeTime(session.updated_at)}</span>
              {(session.total_input_tokens || session.total_output_tokens) ? (
                <>
                  <span className="text-gray-600">&middot;</span>
                  <span className="shrink-0" title="Input / Output tokens">
                    {formatTokenCount(session.total_input_tokens || 0)}&darr; {formatTokenCount(session.total_output_tokens || 0)}&uarr;
                  </span>
                </>
              ) : null}
            </div>
          </div>

          {isHovered && (
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={handleStartRename}
                className="p-1 text-gray-400 hover:text-white rounded hover:bg-gray-600 transition-colors"
                title="Rename"
              >
                <Pencil className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={handleDeleteClick}
                className="p-1 text-gray-400 hover:text-red-400 rounded hover:bg-gray-600 transition-colors"
                title="Delete"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </Link>

      <ConfirmDialog
        isOpen={showDeleteConfirm}
        onClose={() => setShowDeleteConfirm(false)}
        onConfirm={handleDelete}
        title="Delete Session"
        message="Are you sure you want to delete this session? This action cannot be undone."
        confirmLabel="Delete"
        variant="danger"
      />
    </>
  );
}
