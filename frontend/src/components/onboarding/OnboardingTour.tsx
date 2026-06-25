import { useEffect, useLayoutEffect, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useAuthStore } from '@/store/admin';
import { useOnboardingStore } from '@/store/onboarding';

// First-run guided tour for NON-ADMIN users: spotlights the real composer affordances
// one by one (connectors → MCP → skills). Built without a tour lib — each step targets
// live elements by their data-tour anchor and dims everything else around their union
// rect. Shown once per browser (see useOnboardingStore); admins never see it.

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
  // Gate on a known, non-admin user so the tour never flashes before auth resolves
  // (user === null) nor for admins.
  const active = !!user && !user.is_admin && !completed;

  const [index, setIndex] = useState(0);
  const [rect, setRect] = useState<Box | null>(null);
  // Transitions are armed only by a deliberate step change (Next/Back) — never on the
  // first placement or on layout-settling remeasures — so the popup appears directly at
  // step 1 and only glides when the user navigates between steps.
  const [animate, setAnimate] = useState(false);
  // Stays invisible until the target's box is stable across frames, so the overlay's
  // first *visible* paint is already at its final position — the centred composer
  // re-settles for a frame or two on load, which would otherwise show as a jump.
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    if (!active) return;
    // Re-measure every frame while the tour is open: the anchored elements are found by
    // identity (their data-tour name, never by position), and reading their live rect
    // each frame self-heals against late layout, scrolling, and stale Fast-Refresh DOM.
    // The sameBox guard keeps it a no-op render unless the box actually moved.
    let raf = 0;
    let prev: Box | null = null;
    let frames = 0;
    const tick = () => {
      const box = unionRect(STEPS[index].selector);
      setRect((p) => (sameBox(p, box) ? p : box));
      frames += 1;
      // Reveal once the box is stable across two frames (or after a few frames as a
      // fallback, so a never-settling layout can't keep the tour hidden).
      if (box && (sameBox(prev, box) || frames >= 4)) setReady(true);
      prev = box;
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [active, index]);

  // On (re)activation — first mount, or a "Replay tutorial" while still mounted — always
  // restart at step 1, glide disarmed and hidden until re-measured, so a replay never
  // resumes mid-tour.
  useEffect(() => {
    if (active) {
      setIndex(0);
      setAnimate(false);
      setReady(false);
    }
  }, [active]);

  if (!active) return null;

  const tx = animate ? ' transition-all duration-300 ease-out' : '';

  const step = STEPS[index];
  const isLast = index === STEPS.length - 1;
  // Arm the glide on a real navigation only (see `animate`).
  const next = () => {
    if (isLast) return complete();
    setAnimate(true);
    setIndex((i) => i + 1);
  };
  const back = () => {
    setAnimate(true);
    setIndex((i) => Math.max(0, i - 1));
  };

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
    <div
      className={`fixed inset-0 z-[1000] transition-opacity duration-150 ${
        ready ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* Click-blocker: the page is driven via the bubble buttons only. */}
      <div className="absolute inset-0" />
      {/* Spotlight: a transparent hole punched out of a full-screen dim via box-shadow. */}
      {spot && (
        <div
          className={`absolute rounded-xl ring-2 ring-blue-400${tx}`}
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
        role="dialog"
        aria-label="Inkwest tutorial"
        className={`absolute w-80 max-w-[calc(100vw-1rem)] rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-xl${tx}`}
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
