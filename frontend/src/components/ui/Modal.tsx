import { HTMLAttributes, forwardRef, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { clsx } from 'clsx';
import { X } from 'lucide-react';

// Stack of currently-open modals so stacked modals behave: only the TOPMOST responds to
// Escape (otherwise every modal's document-level listener fires and they all close), and the
// body scroll-lock is released only when the LAST modal closes.
const modalStack: symbol[] = [];

export interface ModalProps extends HTMLAttributes<HTMLDivElement> {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  size?: 'sm' | 'md' | 'lg' | 'xl' | '2xl';
  /**
   * Vertical alignment. 'top' keeps the modal anchored to a fixed top offset
   * so content-height changes (e.g. live filtering) don't make it jump.
   */
  align?: 'center' | 'top';
  /**
   * Override the body's scroll/layout. Default is `overflow-y-auto` (the whole
   * body scrolls). Pass e.g. `flex flex-col overflow-hidden` when the content
   * manages its own internal scroll (pinned header/footer + a scrolling middle).
   */
  bodyClassName?: string;
}

export const Modal = forwardRef<HTMLDivElement, ModalProps>(
  ({ className, isOpen, onClose, title, size = 'md', align = 'center', bodyClassName, children, ...props }, ref) => {
    // Stable identity for this modal instance in the stack.
    const idRef = useRef<symbol>();
    if (!idRef.current) idRef.current = Symbol('modal');

    useEffect(() => {
      if (!isOpen) return;
      const id = idRef.current!;
      modalStack.push(id);
      document.body.style.overflow = 'hidden';

      const handleEscape = (e: KeyboardEvent) => {
        if (e.key !== 'Escape') return;
        // Only the topmost modal reacts — stacked modals share the document, so without this
        // every open modal would close at once.
        if (modalStack[modalStack.length - 1] !== id) return;
        // stopImmediate so the global Escape-to-stop listener (ChatInput) doesn't also fire
        // and abort a live stream, and no lower modal listener runs.
        e.stopImmediatePropagation();
        onClose();
      };
      document.addEventListener('keydown', handleEscape);

      return () => {
        document.removeEventListener('keydown', handleEscape);
        const i = modalStack.lastIndexOf(id);
        if (i !== -1) modalStack.splice(i, 1);
        // Release the scroll-lock only once the last modal is gone (a stacked modal above may
        // have closed while one below is still open).
        if (modalStack.length === 0) document.body.style.overflow = 'unset';
      };
    }, [isOpen, onClose]);

    if (!isOpen) return null;

    const sizes = {
      sm: 'max-w-md',
      md: 'max-w-lg',
      lg: 'max-w-2xl',
      xl: 'max-w-4xl',
      '2xl': 'max-w-6xl',
    };

    const alignments = {
      center: 'items-center',
      // Fixed top offset: the modal's top edge stays put when its content
      // grows/shrinks (e.g. while filtering a list), instead of re-centering.
      // pt-12 (3rem) matches the bottom gap left by the cap below (6rem total →
      // 3rem top + 3rem bottom), so a maxed-out modal sits symmetrically.
      top: 'items-start pt-12',
    };

    // The dialog cap must leave room for the wrapper's vertical chrome, else the
    // dialog overflows the viewport and the outer container (overflow-y-auto)
    // grows a second scrollbar with the footer pushed off-screen. 'top' adds a
    // fixed pt-12 offset, so it needs a larger cap (pt-12 + matching 3rem bottom
    // gap → 6rem). 'center' keeps the long-standing 90vh (it has its own slack).
    const maxHeights = {
      center: 'max-h-[90vh]',
      top: 'max-h-[calc(100vh-6rem)]',
    };

    // Full-viewport stage so the dialog centers on the whole screen (not offset to the
    // chat region right of the sidebar).
    return createPortal(
      <div className="fixed inset-0 z-50 overflow-y-auto">
        <div className={clsx('flex min-h-full justify-center p-4', alignments[align])}>
          <div
            className="fixed inset-0 bg-black/70 transition-opacity"
            onClick={onClose}
          />

          <div
            ref={ref}
            role="dialog"
            aria-modal="true"
            className={clsx(
              'relative w-full bg-gray-800 text-white rounded-lg shadow-xl',
              // flex column + capped height so tall content scrolls inside the
              // body instead of overflowing the viewport.
              'flex flex-col overflow-hidden',
              maxHeights[align],
              sizes[size],
              className
            )}
            {...props}
          >
            {title && (
              <div className="flex items-center justify-between px-6 py-4 border-b border-gray-700 shrink-0">
                <h3 className="text-lg font-semibold text-white">{title}</h3>
                <button
                  onClick={onClose}
                  className="text-gray-400 hover:text-white transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            )}

            <div className={clsx('px-6 py-4 flex-1 min-h-0', bodyClassName ?? 'overflow-y-auto')}>
              {children}
            </div>
          </div>
        </div>
      </div>,
      document.body,
    );
  }
);

Modal.displayName = 'Modal';
