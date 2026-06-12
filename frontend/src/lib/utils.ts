import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(date: string): string {
  return new Date(date).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * Per-message timestamp: 24h `HH:MM:SS`. The ISO calendar date (`YYYY-MM-DD`) is
 * prefixed only when the message is not from today, so same-day conversations
 * stay terse while older messages remain unambiguous. Date parts are taken from
 * local time (not `toISOString`, which is UTC and could show the wrong day).
 */
export function formatTimestamp(date: string): string {
  const d = new Date(date);
  const now = new Date();
  const isToday =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (isToday) return localTime(d);
  return `${localIsoDate(d)} ${localTime(d)}`;
}

/**
 * Full local timestamp `YYYY-MM-DD HH:MM:SS`, always with the date and seconds.
 * Used as the hover overlay (title) on per-message timestamps so the full date
 * is reachable even when the inline form omits it for today's messages.
 */
export function formatFullTimestamp(date: string): string {
  const d = new Date(date);
  return `${localIsoDate(d)} ${localTime(d)}`;
}

/** Local wall-clock time as 24h `HH:MM:SS`. */
function localTime(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Local calendar date as `YYYY-MM-DD` (not UTC — avoids off-by-one near midnight). */
function localIsoDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Compact mode font size classes (centralized to keep UI consistent) */
export const TEXT_SIZE = {
  body: (compact: boolean) => compact ? 'text-[13px]' : 'text-sm',
  code: (compact: boolean) => compact ? 'text-[12px]' : 'text-sm',
  label: (compact: boolean) => compact ? 'text-[10px]' : 'text-xs',
  title: (compact: boolean) => compact ? 'text-[11px]' : 'text-sm',
} as const;

export function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function formatRelativeTime(date: string): string {
  const now = new Date();
  const then = new Date(date);
  const diffMs = now.getTime() - then.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return formatDate(date);
}
