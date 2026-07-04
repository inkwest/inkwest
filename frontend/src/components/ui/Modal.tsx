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

// What the focus trap counts as tabbable inside the dialog. tabindex="-1" is excluded on
// every branch (an element removed from the tab order must not become a wrap target —
// focus() on it would succeed but the user couldn't Tab away from it as an edge).
// Known limitation: visibility (display:none etc.) isn't checked; keep dialog edges free
// of hidden focusables.
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]',
]
  .map((s) => `${s}:not([tabindex="-1"])`)
  .join(', ');

export const Modal = forwardRef<HTMLDivElement, ModalProps>(
  ({ className, isOpen, onClose, title, size = 'md', align = 'center', bodyClassName, children, ...props }, ref) => {
    // Stable identity for this modal instance in the stack.
    const idRef = useRef<symbol>();
    if (!idRef.current) idRef.current = Symbol('modal');

    // Internal handle on the dialog node for the focus trap; the forwarded ref (public
    // API) is fed from the same callback so both stay in sync.
    const dialogRef = useRef<HTMLDivElement | null>(null);
    const setDialogRef = (node: HTMLDivElement | null) => {
      dialogRef.current = node;
      if (typeof ref === 'function') ref(node);
      else if (ref) ref.current = node;
    };

    // Restore focus to the element that had it when the modal opened (WCAG 2.4.3) —
    // its own effect keyed on isOpen only, so a mid-open onClose identity change can't
    // re-capture an element inside the dialog as the "opener".
    useEffect(() => {
      if (!isOpen) return;
      const opener = document.activeElement as HTMLElement | null;
      return () => {
        if (opener && document.contains(opener)) opener.focus();
      };
    }, [isOpen]);

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

      // Keyboard focus trap (topmost modal only): the overlay blocks clicks but not Tab,
      // so without this, focus escapes to controls behind the modal and Enter activates
      // them "through" it (WCAG 2.4.3). Tab cycles inside the dialog; a Tab from outside
      // (focus never entered, or was lost) pulls focus in.
      const handleTab = (e: KeyboardEvent) => {
        if (e.key !== 'Tab') return;
        if (modalStack[modalStack.length - 1] !== id) return;
        const dialog = dialogRef.current;
        if (!dialog) return;
        const focusables = dialog.querySelectorAll<HTMLElement>(FOCUSABLE);
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement;
        if (!dialog.contains(active)) {
          // A foreign overlay dialog (e.g. the onboarding tour bubble — role="dialog"
          // but not a Modal) owns the focus: leave its Tab alone instead of yanking
          // focus in. Modal-managed dialogs carry data-modal, so focus left in a lower
          // stacked modal IS still pulled into the topmost one.
          if (active?.closest('[role="dialog"]:not([data-modal])')) return;
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && active === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault();
          first.focus();
        }
      };
      document.addEventListener('keydown', handleTab, true);

      return () => {
        document.removeEventListener('keydown', handleEscape);
        document.removeEventListener('keydown', handleTab, true);
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
            ref={setDialogRef}
            role="dialog"
            aria-modal="true"
            data-modal=""
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
