import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Modal } from './Modal';

describe('Modal focus trap', () => {
  // No title → no header X button, so the two content buttons are the only focusables
  // and the wrap-around edges are unambiguous.
  const setup = () => {
    render(
      <Modal isOpen onClose={vi.fn()}>
        <button>first</button>
        <button>last</button>
      </Modal>,
    );
    return {
      first: screen.getByRole('button', { name: 'first' }),
      last: screen.getByRole('button', { name: 'last' }),
    };
  };

  it('Tab from the last focusable wraps to the first', () => {
    const { first, last } = setup();
    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(first).toHaveFocus();
  });

  it('Shift+Tab from the first focusable wraps to the last', () => {
    const { first, last } = setup();
    first.focus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();
  });

  it('Tab with focus outside the dialog pulls focus in', () => {
    const { first } = setup();
    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: 'Tab' });
    expect(first).toHaveFocus();
  });

  it('Tab between inner focusables is left to the browser (no preventDefault)', () => {
    const { first } = setup();
    first.focus();
    // jsdom doesn't move focus natively; assert the trap didn't preventDefault (fireEvent
    // returns true), i.e. the browser's normal Tab order runs inside the dialog.
    expect(fireEvent.keyDown(first, { key: 'Tab' })).toBe(true);
  });
});
