import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDialog } from './ConfirmDialog';

describe('ConfirmDialog', () => {
  const setup = () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        isOpen
        onClose={onClose}
        onConfirm={onConfirm}
        title="Delete session"
        message="This cannot be undone."
      />,
    );
    return { onClose, onConfirm };
  };

  it('autofocuses the confirm button and Enter confirms', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();
    expect(screen.getByRole('button', { name: 'Confirm' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('Space confirms like Enter', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();
    await user.keyboard(' ');
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('Enter on the focused Cancel button cancels instead of confirming', async () => {
    const user = userEvent.setup();
    const { onClose, onConfirm } = setup();
    screen.getByRole('button', { name: 'Cancel' }).focus();
    await user.keyboard('{Enter}');
    expect(onConfirm).not.toHaveBeenCalled();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('a held Enter (key repeat) neither confirms nor natively clicks the button', () => {
    const { onConfirm } = setup();
    // fireEvent returns false when preventDefault was called — the repeat must be
    // swallowed entirely so it can't auto-confirm a dialog it just opened.
    const notPrevented = fireEvent.keyDown(screen.getByRole('button', { name: 'Confirm' }), {
      key: 'Enter',
      repeat: true,
    });
    expect(notPrevented).toBe(false);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
