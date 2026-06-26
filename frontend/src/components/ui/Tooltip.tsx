import { useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/lib/utils';

interface Props {
  /** Tooltip body. A plain string also becomes the trigger's aria-label. */
  content: ReactNode;
  /** The hover target. The tooltip shows while the pointer is anywhere over it. */
  children: ReactNode;
  /** Extra classes for the wrapper that hosts the children + hover handlers. */
  className?: string;
}

const OFFSET = 14; // gap between the cursor and the tooltip box

/**
 * Cursor-following hover tooltip. Renders through a portal with `position: fixed`
 * so it escapes scroll/overflow containers (a CSS-only in-flow tooltip would be
 * clipped by an `overflow-auto` ancestor). Info-only: `pointer-events-none` so it
 * never steals the hover from the content underneath. Flips toward screen center
 * when the pointer is near the right/bottom edge so the box stays on-screen.
 */
export function Tooltip({ content, children, className }: Props) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  // Anchor the box's edge to the cursor, flipping side near the viewport edge.
  const onRight = pos ? pos.x > window.innerWidth * 0.6 : false;
  const onBottom = pos ? pos.y > window.innerHeight * 0.75 : false;

  return (
    <div
      className={className}
      aria-label={typeof content === 'string' ? content : undefined}
      onMouseMove={(e) => setPos({ x: e.clientX, y: e.clientY })}
      onMouseLeave={() => setPos(null)}
    >
      {children}
      {pos &&
        createPortal(
          <div
            role="tooltip"
            className={cn(
              'pointer-events-none fixed z-50 max-w-xs rounded-md border border-gray-700',
              'bg-gray-800 px-2 py-1 text-xs text-gray-100 shadow-lg'
            )}
            style={{
              left: pos.x + (onRight ? -OFFSET : OFFSET),
              top: pos.y + (onBottom ? -OFFSET : OFFSET),
              transform: `translate(${onRight ? '-100%' : '0'}, ${onBottom ? '-100%' : '0'})`,
            }}
          >
            {content}
          </div>,
          document.body
        )}
    </div>
  );
}
