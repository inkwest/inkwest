import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { X } from 'lucide-react';
import type { Components } from 'react-markdown';
import { useAnnouncementsStore } from '@/store/announcements';
import type { Announcement, AnnouncementLevel } from '@/types/api';

const DISMISSED_STORAGE_KEY = 'inkwest.announcements.dismissed.v1';

// setTimeout treats a delay above 2^31-1 ms as 0: a far expiry must not fire at once.
const MAX_TIMEOUT_MS = 2 ** 31 - 1;

// Tailwind blue-600, green-700, amber-500, red-600 as hex so opacity can go through rgba().
// green-700, not 600: white on green-600 is 3.3:1, below AA.
const LEVEL_BG: Record<AnnouncementLevel, string> = {
  info: '#2563eb',
  success: '#15803d',
  warning: '#f59e0b',
  danger: '#dc2626',
};

// A translucent bar is seen over ShellLayout's bg-gray-900, so text is picked on the blend.
const SHELL_BG = [0x11, 0x18, 0x27];

function rgbOf(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// Black or white, whichever contrasts more (WCAG) with the color as drawn: the max of the two
// is always >= 4.58:1, and at 100% it gives each level its own text (white, black on amber).
function textColorFor(hex: string, percent: number): 'black' | 'white' {
  const alpha = percent / 100;
  const [r, g, b] = rgbOf(hex).map((c, i) => {
    const s = (c * alpha + SHELL_BG[i] * (1 - alpha)) / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) ? 'black' : 'white';
}

function withOpacity(hex: string, percent: number): string {
  const rgb = rgbOf(hex).join(', ');
  return percent >= 100 ? `rgb(${rgb})` : `rgba(${rgb}, ${percent / 100})`;
}

const dismissalKey = (a: Announcement) => `${a.id}:${a.updated_at}`;
const expiresAt = (a: Announcement) => (a.ends_at ? Date.parse(a.ends_at) : NaN);

// Storage is best effort: private mode, quota or a corrupt value only lose the persistence.
function readDismissed(): Set<string> {
  try {
    // A non-iterable value (object, number) throws here and lands in the catch.
    return new Set<string>(JSON.parse(window.localStorage.getItem(DISMISSED_STORAGE_KEY) ?? '[]'));
  } catch {
    return new Set();
  }
}

function writeDismissed(keys: Set<string>) {
  try {
    window.localStorage.setItem(DISMISSED_STORAGE_KEY, JSON.stringify([...keys]));
  } catch {
    // Still hidden for this page.
  }
}

// Inline markdown only (004-AC8): block elements are unwrapped to their text, images dropped.
// Raw HTML stays text: react-markdown has no rehype-raw here.
const ALLOWED_ELEMENTS = ['strong', 'em', 'code', 'a', 'del'];
const remarkPlugins = [remarkGfm];
const safeUrl = (url: string) => (/^(https?|mailto):/i.test(url) ? url : undefined);
const components: Components = {
  a: ({ href, children }) =>
    href ? (
      <a href={href} target="_blank" rel="noopener noreferrer" className="underline font-medium">
        {children}
      </a>
    ) : (
      <>{children}</>
    ),
  code: ({ children }) => <code className="rounded bg-black/20 px-1 font-mono text-[0.9em]">{children}</code>,
};

// Admin-authored notices (spec 004), oldest first. Renders nothing when there is nothing to show.
export function AnnouncementBanners() {
  const items = useAnnouncementsStore((s) => s.items);
  const loaded = useAnnouncementsStore((s) => s.loaded);
  const refresh = useAnnouncementsStore((s) => s.refresh);
  const [dismissed, setDismissed] = useState(readDismissed);
  // Bumped by the expiry timer to re-render at the nearest ends_at.
  const [tick, setTick] = useState(0);

  // The admin console has no sync socket: regaining focus is its refresh trigger (004-AC3).
  useEffect(() => {
    void refresh();
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [refresh]);

  // Another tab closed a banner: hide it here too.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === DISMISSED_STORAGE_KEY) setDismissed((current) => new Set([...current, ...readDismissed()]));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  // Prune only once a fetch has answered, else a page load wipes dismissals before the response.
  // Storage is re-read, not taken from memory: other tabs write to it too.
  useEffect(() => {
    if (!loaded) return;
    const served = new Set(items.map(dismissalKey));
    const stored = readDismissed();
    const kept = new Set([...stored].filter((k) => served.has(k)));
    if (kept.size !== stored.size) writeDismissed(kept);
  }, [items, loaded]);

  // Fresh Date.now(), not a stored "now": a stale baseline would delay a later banner's expiry.
  useEffect(() => {
    const now = Date.now();
    const next = Math.min(...items.map(expiresAt).filter((t) => t > now));
    if (!Number.isFinite(next)) return;
    const timer = setTimeout(() => setTick((n) => n + 1), Math.min(next - now, MAX_TIMEOUT_MS));
    return () => clearTimeout(timer);
  }, [items, tick]);

  // Merge into the fresh stored set so a dismissal another tab wrote survives; memory keeps the
  // union so a close storage refused still holds for this page.
  const dismiss = (a: Announcement) => {
    const next = readDismissed().add(dismissalKey(a));
    writeDismissed(next);
    setDismissed((current) => new Set([...current, ...next]));
  };

  const now = Date.now();
  const visible = items.filter((a) => !dismissed.has(dismissalKey(a)) && !(expiresAt(a) <= now));
  if (visible.length === 0) return null;

  return (
    <>
      {visible.map((a) => {
        const bg = a.color ?? LEVEL_BG[a.level];
        return (
          <div
            key={a.id}
            role="status"
            data-level={a.level}
            className="px-4 py-2 flex items-center gap-2 text-sm"
            style={{ backgroundColor: withOpacity(bg, a.opacity), color: a.text_color ?? textColorFor(bg, a.opacity) }}
          >
            <span className="flex-1 min-w-0 break-words">
              <ReactMarkdown
                remarkPlugins={remarkPlugins}
                allowedElements={ALLOWED_ELEMENTS}
                unwrapDisallowed
                urlTransform={safeUrl}
                components={components}
              >
                {a.message}
              </ReactMarkdown>
            </span>
            {a.dismissible && (
              <button
                type="button"
                onClick={() => dismiss(a)}
                aria-label="Dismiss announcement"
                className="shrink-0 opacity-70 hover:opacity-100"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        );
      })}
    </>
  );
}
