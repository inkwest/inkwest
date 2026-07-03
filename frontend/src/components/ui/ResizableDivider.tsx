import { useEffect, useRef } from 'react';

interface Props {
  // Called with the horizontal pixel delta as the handle is dragged. The parent grows/shrinks
  // the pane on its LEFT by this delta (the pane on the right is expected to flex-fill the rest).
  onResize: (deltaX: number) => void;
  // Optional: reset to the default width (wired to double-click — a common splitter affordance).
  onReset?: () => void;
  label?: string;
}

/**
 * Draggable vertical splitter between two panes. Window-level move/up listeners are bound once
 * (not per-render) and only act while a drag is live; the latest onResize is read through a ref
 * so a re-rendering parent never re-subscribes. Body cursor/selection are locked during the drag
 * so the pointer stays a col-resize and text isn't selected across panes.
 */
export function ResizableDivider({ onResize, onReset, label = 'Resize panels' }: Props) {
  const draggingRef = useRef(false);
  const lastXRef = useRef(0);
  const onResizeRef = useRef(onResize);
  onResizeRef.current = onResize;

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current) return;
      const dx = e.clientX - lastXRef.current;
      lastXRef.current = e.clientX;
      if (dx !== 0) onResizeRef.current(dx);
    };
    const stop = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', stop);
    // A mouseup released OUTSIDE the window is never delivered; `blur` catches that case so a
    // drag can't get stuck live with the body cursor/selection locked globally.
    window.addEventListener('blur', stop);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', stop);
      window.removeEventListener('blur', stop);
      stop(); // release the body locks if we unmount mid-drag
    };
  }, []);

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      onMouseDown={(e) => {
        e.preventDefault(); // don't start a text selection
        draggingRef.current = true;
        lastXRef.current = e.clientX;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
      }}
      onDoubleClick={onReset}
      // 8px hit area with a hairline centred in it; the line thickens/colours on hover + drag.
      className="group relative flex w-2 shrink-0 cursor-col-resize items-stretch justify-center"
    >
      <span className="w-px bg-gray-700 transition-colors group-hover:bg-blue-500" />
    </div>
  );
}
