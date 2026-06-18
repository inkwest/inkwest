import { useEffect, useState } from 'react';
import { X, FileText } from 'lucide-react';
import { Attachment } from '@/types/api';
import { fetchAttachmentBlob } from '@/services/api';

function isImage(contentType: string): boolean {
  return contentType.startsWith('image/');
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// Composer: pending attachments (local image preview, removable chips).
// ---------------------------------------------------------------------------

// A just-uploaded attachment plus the object URL of the local File, so an image
// preview shows without a round-trip back to the server.
export interface PendingAttachment extends Attachment {
  previewUrl?: string;
}

export function AttachmentChips({
  attachments,
  onRemove,
}: {
  attachments: PendingAttachment[];
  onRemove: (filename: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-2">
      {attachments.map((a) => (
        <div
          key={a.filename}
          className="flex items-center gap-2 rounded-lg border border-gray-600 bg-gray-700/60 py-1 pl-1 pr-2"
          title={a.original_name}
        >
          {a.previewUrl ? (
            <img src={a.previewUrl} alt={a.original_name} className="h-8 w-8 rounded object-cover" />
          ) : (
            <span className="flex h-8 w-8 items-center justify-center rounded bg-gray-800">
              <FileText className="h-4 w-4 text-gray-300" />
            </span>
          )}
          <span className="max-w-[10rem] truncate text-xs text-gray-200">{a.original_name}</span>
          <span className="text-[10px] text-gray-400">{formatSize(a.size)}</span>
          <button
            type="button"
            onClick={() => onRemove(a.filename)}
            aria-label={`Remove ${a.original_name}`}
            className="ml-1 rounded p-0.5 text-gray-400 hover:bg-gray-600 hover:text-white"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// History: attachments rendered on a sent user message.
// ---------------------------------------------------------------------------

function FileChip({ attachment }: { attachment: Attachment }) {
  return (
    <span
      className="flex items-center gap-2 rounded-lg border border-gray-600 bg-gray-800/60 px-2 py-1"
      title={attachment.original_name}
    >
      <FileText className="h-4 w-4 shrink-0 text-gray-300" />
      <span className="max-w-[12rem] truncate text-xs text-gray-200">{attachment.original_name}</span>
      <span className="text-[10px] text-gray-400">{formatSize(attachment.size)}</span>
    </span>
  );
}

// An <img> can't carry the bearer token, so fetch the bytes with auth and show
// them via an object URL. Falls back to a file chip if the fetch fails.
function HistoryImage({ sessionId, attachment }: { sessionId: string; attachment: Attachment }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    fetchAttachmentBlob(sessionId, attachment.filename)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        /* leave url null → file chip fallback */
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [sessionId, attachment.filename]);

  if (!url) return <FileChip attachment={attachment} />;
  return (
    <img
      src={url}
      alt={attachment.original_name}
      title={attachment.original_name}
      className="max-h-40 max-w-[12rem] rounded-lg border border-gray-600 object-contain"
    />
  );
}

export function MessageAttachments({
  sessionId,
  attachments,
}: {
  sessionId: string;
  attachments: Attachment[];
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {attachments.map((a) =>
        isImage(a.content_type) ? (
          <HistoryImage key={a.filename} sessionId={sessionId} attachment={a} />
        ) : (
          <FileChip key={a.filename} attachment={a} />
        ),
      )}
    </div>
  );
}
