import { useRef } from 'react';
import { Button } from './Button';
import { Modal } from './Modal';
import { useConfirmKey } from '@/hooks/useConfirmKey';

interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'warning' | 'default';
}

export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
}: ConfirmDialogProps) {
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  // Enter/Space confirm with the primary button autofocused; the hook hardens against
  // key-repeat, same-keystroke and Enter-on-Cancel bypasses (see useConfirmKey).
  useConfirmKey(isOpen, confirmButtonRef, onConfirm, { space: true });

  const buttonVariant = variant === 'danger' ? 'danger' : 'primary';

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="sm">
      <p className="text-gray-300 text-sm mb-6">{message}</p>
      <div className="flex justify-end gap-3">
        <Button variant="secondary" onClick={onClose}>
          {cancelLabel}
        </Button>
        <Button
          ref={confirmButtonRef}
          variant={buttonVariant}
          onClick={onConfirm}
        >
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}
