import { HTMLAttributes, forwardRef, useEffect, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { clsx } from 'clsx';
import { X } from 'lucide-react';
import { useSettingsStore } from '@/store/settings';

export interface ModalProps extends HTMLAttributes<HTMLDivElement> {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  size?: 'sm' | 'md' | 'lg' | 'xl';
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
    // Center over the content area, not the whole window: offset the stage by the live
    // sidebar width (+4px resize handle) on desktop. The .modal-stage CSS only applies
    // the offset at md+, so mobile (overlay sidebar) stays full-width.
    const sidebarWidth = useSettingsStore((s) => s.sidebarWidth);
    useEffect(() => {
      const handleEscape = (e: KeyboardEvent) => {
        // Consume Escape here so the topmost overlay wins — otherwise it bubbles to the
        // global Escape-to-stop listener (ChatInput) and would also abort a live stream.
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
        }
      };

      if (isOpen) {
        document.addEventListener('keydown', handleEscape);
        document.body.style.overflow = 'hidden';
      }

      return () => {
        document.removeEventListener('keydown', handleEscape);
        document.body.style.overflow = 'unset';
      };
    }, [isOpen, onClose]);

    if (!isOpen) return null;

    const sizes = {
      sm: 'max-w-md',
      md: 'max-w-lg',
      lg: 'max-w-2xl',
      xl: 'max-w-4xl',
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

    return createPortal(
      <div
        className="modal-stage fixed inset-y-0 right-0 z-50 overflow-y-auto"
        style={{ '--modal-left': `${sidebarWidth + 4}px` } as CSSProperties}
      >
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
