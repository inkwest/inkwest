import { useEffect, useRef, type RefObject } from 'react';

// Document-level "Enter confirms the dialog" listener, shared by the confirm-style
// modals (ConfirmDialog, ChatInput's no-connector confirm) so they can't drift apart
// again. Hardened against the keyboard bypasses that let a send slip past the popup:
//
// - CAPTURE phase: a listener attached while the opening keystroke is still dispatching
//   (React 18 flushes effects synchronously for discrete events) can never see that same
//   event — its document-capture phase has already passed. The bubble-phase variant
//   confirmed the dialog with the very keystroke that opened it.
// - e.repeat swallowed WITH preventDefault: a held Enter key-repeats onto the autofocused
//   confirm button and would otherwise natively activate it, auto-confirming the dialog.
// - A focused text field owns its keys (e.g. the chat composer behind the modal): never
//   confirm from inside one.
// - Focus on another interactive control (Cancel, the X close, a link): its own
//   Enter→click activation wins — otherwise Enter on Cancel would CONFIRM.
// Stack of open confirm dialogs so only the TOPMOST reacts to Enter — mirrors Modal's
// modalStack. Without it, stacked confirms would all fire on a single keystroke.
const confirmStack: symbol[] = [];

export function useConfirmKey(
  open: boolean,
  confirmRef: RefObject<HTMLElement | null>,
  onConfirm: () => void,
  { space = false }: { space?: boolean } = {},
) {
  // Stable identity for this dialog instance in the stack.
  const idRef = useRef<symbol>();
  if (!idRef.current) idRef.current = Symbol('confirm');

  // Latest callback without re-binding the listener (it reads live state upstream).
  const cbRef = useRef(onConfirm);
  cbRef.current = onConfirm;

  // Autofocus only on the open TRANSITION — re-running on every render would yank
  // focus back to the confirm button whenever unrelated state churns (store updates,
  // streaming flags) while the user has tabbed elsewhere.
  useEffect(() => {
    if (open) confirmRef.current?.focus();
  }, [open, confirmRef]);

  useEffect(() => {
    if (!open) return;
    const id = idRef.current!;
    confirmStack.push(id);
    const onKey = (e: KeyboardEvent) => {
      if (confirmStack[confirmStack.length - 1] !== id) return;
      if (e.key !== 'Enter' && !(space && e.key === ' ')) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      const ctrl = t?.closest('a,button,[role="button"]');
      if (ctrl && ctrl !== confirmRef.current) return;
      e.preventDefault();
      if (e.repeat) return;
      cbRef.current();
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      const i = confirmStack.lastIndexOf(id);
      if (i !== -1) confirmStack.splice(i, 1);
    };
  }, [open, space, confirmRef]);
}
