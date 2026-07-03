import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useAuthStore } from '@/store/admin';
import { useOnboardingStore } from '@/store/onboarding';

// First-run guided tour for all users: spotlights the real composer affordances
// one step at a time (attachments → skills → connectors → MCP → model). Built without a
// tour lib — each step targets live elements by their data-tour anchor (by identity,
// never by position) and dims everything else around their bounding box. Shown once per
// browser (see useOnboardingStore); replayable from the header menu.

interface Step {
  // Matches one or more live elements; the spotlight covers their union bounding box.
  selector: string;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    selector: '[data-tour="attach"]',
    title: 'Attachments',
    body: 'Attach files — logs, configs, screenshots, code — to your message for the assistant to read and analyze.',
  },
  {
    selector: '[data-tour="skills"]',
    title: 'Skills',
    body: 'Type "/" in the message, or click this icon, to insert a skill: a ready-made prompt you can tweak before sending.',
  },
  {
    selector: '[data-tour="connectors"]',
    title: 'Connectors',
    body: 'Give the assistant read-only access to AWS or GitLab for this conversation. Clicking opens an authentication popup; access stays scoped to the session and can be revoked anytime.',
  },
  {
    selector: '[data-tour="mcp"]',
    title: 'MCP servers',
    body: 'Connect MCP servers (observability, and more) to give the assistant extra tools for the duration of this conversation.',
  },
  {
    selector: '[data-tour="model"]',
    title: 'Model',
    body: 'Pick the model (and engine) this conversation runs on. Leave it on the default to follow the workspace default.',
  },
];

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

// Union bounding box of every element matching the selector (viewport coords), or null
// when none is present (e.g. a connector disabled for this user) — the step then just
// centres its bubble instead of pointing at nothing.
function unionRect(selector: string): Box | null {
  const els = Array.from(document.querySelectorAll(selector));
  if (els.length === 0) return null;
  let top = Infinity;
  let left = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    top = Math.min(top, r.top);
    left = Math.min(left, r.left);
    right = Math.max(right, r.right);
    bottom = Math.max(bottom, r.bottom);
  }
  return { top, left, width: right - left, height: bottom - top };
}

function sameBox(a: Box | null, b: Box | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;
}

const PAD = 6; // breathing room around the spotlighted element
const BUBBLE_W = 320;

export function OnboardingTour() {
  const user = useAuthStore((s) => s.user);
  const completed = useOnboardingStore((s) => s.completed);
  const complete = useOnboardingStore((s) => s.complete);
  // Gate on a known user so the tour never flashes before auth resolves (user === null).
  const active = !!user && !completed;

  const [index, setIndex] = useState(0);
  const [rect, setRect] = useState<Box | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Reliable positioning: measure the CURRENT step's anchor synchronously whenever the
  // step changes. useLayoutEffect runs after the DOM commit but before paint, so the
  // layout is final and the very first paint is already correct (no flash, no race).
  // Re-measure only on real layout changes — window resize/scroll and a ResizeObserver
  // on the document. No per-frame polling loop: that raced with step changes (a frame
  // could fire with the previous step's index) and intermittently spotlighted the wrong
  // control. Anchors are matched by identity (their data-tour name), never by position.
  useLayoutEffect(() => {
    if (!active) return;
    const measure = () => {
      const box = unionRect(STEPS[index].selector);
      setRect((p) => (sameBox(p, box) ? p : box));
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true); // capture: catch nested scrollers
    let ro: ResizeObserver | undefined;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(measure); // catches reflows that shift the anchor (e.g. async config load)
      ro.observe(document.documentElement);
    }
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
      ro?.disconnect();
    };
  }, [active, index]);

  // On (re)activation — first mount, or a "Replay tutorial" while still mounted — restart
  // at step 1 so a replay never resumes mid-tour.
  useEffect(() => {
    if (active) setIndex(0);
  }, [active]);

  // Enter advances the tour (last step → done). Capture phase + stop/prevent so the key
  // never reaches the composer behind the overlay, whose Enter handler would send a
  // message. When focus is on a bubble button (Back/Skip/Next), we bow out and let its
  // native Enter→click run instead — so Back/Skip aren't overridden into "advance". A
  // held Enter (e.repeat) is ignored so it can't blast through the whole tour at once.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Enter' || e.repeat) return;
      if (dialogRef.current?.contains(document.activeElement)) return;
      e.preventDefault();
      e.stopPropagation();
      if (index === STEPS.length - 1) complete();
      else setIndex((i) => i + 1);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [active, index, complete]);

  if (!active) return null;

  const step = STEPS[index];
  const isLast = index === STEPS.length - 1;
  const next = () => (isLast ? complete() : setIndex((i) => i + 1));
  const back = () => setIndex((i) => Math.max(0, i - 1));

  const spot = rect
    ? { top: rect.top - PAD, left: rect.left - PAD, width: rect.width + PAD * 2, height: rect.height + PAD * 2 }
    : null;

  // Bubble: under the target if it fits, otherwise above; centred when there's no target.
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let bubbleStyle: CSSProperties;
  if (spot) {
    const below = spot.top + spot.height + 12;
    const placeBelow = below + 180 < vh; // rough bubble height budget
    const left = Math.min(Math.max(8, spot.left), vw - BUBBLE_W - 8);
    bubbleStyle = placeBelow
      ? { top: below, left }
      : { bottom: vh - spot.top + 12, left };
  } else {
    bubbleStyle = { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' };
  }

  return createPortal(
    <div className="fixed inset-0 z-[1000]">
      {/* Click-blocker: the page is driven via the bubble buttons only. */}
      <div className="absolute inset-0" />
      {/* Spotlight: a transparent hole punched out of a full-screen dim via box-shadow. */}
      {spot && (
        <div
          className="absolute rounded-xl ring-2 ring-blue-400"
          style={{
            top: spot.top,
            left: spot.left,
            width: spot.width,
            height: spot.height,
            boxShadow: '0 0 0 9999px rgba(0,0,0,0.65)',
            pointerEvents: 'none',
          }}
        />
      )}
      {/* Dim the whole screen when there's no target to spotlight. */}
      {!spot && <div className="absolute inset-0 bg-black/65" />}

      <div
        ref={dialogRef}
        role="dialog"
        aria-label="Inkwest tutorial"
        className="absolute w-80 max-w-[calc(100vw-1rem)] rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-xl"
        style={bubbleStyle}
      >
        <div className="mb-1 text-xs font-medium text-blue-400">
          Step {index + 1}/{STEPS.length}
        </div>
        <h3 className="mb-1 text-sm font-semibold text-gray-100">{step.title}</h3>
        <p className="mb-4 text-sm leading-relaxed text-gray-400">{step.body}</p>
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={complete}
            className="text-xs text-gray-500 hover:text-gray-300"
          >
            Skip
          </button>
          <div className="flex gap-2">
            {index > 0 && (
              <button
                type="button"
                onClick={back}
                className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-800"
              >
                Back
              </button>
            )}
            <button
              type="button"
              onClick={next}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500"
            >
              {isLast ? 'Done' : 'Next'}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}
