import { HTMLAttributes, forwardRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { clsx } from 'clsx';
import { X } from 'lucide-react';

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
}

export const Modal = forwardRef<HTMLDivElement, ModalProps>(
  ({ className, isOpen, onClose, title, size = 'md', align = 'center', children, ...props }, ref) => {
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
      top: 'items-start pt-16',
    };

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
              'flex flex-col max-h-[90vh] overflow-hidden',
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

            <div className="px-6 py-4 flex-1 min-h-0 overflow-y-auto">{children}</div>
          </div>
        </div>
      </div>,
      document.body,
    );
  }
);

Modal.displayName = 'Modal';
