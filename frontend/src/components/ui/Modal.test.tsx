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

  it('skips controls disabled through a <fieldset>', () => {
    render(
      <Modal isOpen onClose={vi.fn()}>
        <fieldset disabled>
          <input aria-label="locked" />
        </fieldset>
        <button>save</button>
      </Modal>,
    );
    const save = screen.getByRole('button', { name: 'save' });
    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: 'Tab' });
    expect(save).toHaveFocus();
  });

  it('Tab is swallowed when the dialog has no focusable element', () => {
    render(
      <Modal isOpen onClose={vi.fn()}>
        <p>text only</p>
      </Modal>,
    );
    // Nothing to cycle to: preventDefault (fireEvent returns false) so focus can't
    // escape to controls behind the overlay.
    expect(fireEvent.keyDown(document.body, { key: 'Tab' })).toBe(false);
  });

  it('only the topmost stacked modal traps Tab', () => {
    render(
      <>
        <Modal isOpen onClose={vi.fn()}>
          <button>a1</button>
        </Modal>
        <Modal isOpen onClose={vi.fn()}>
          <button>b1</button>
          <button>b2</button>
        </Modal>
      </>,
    );
    // Focus in the lower modal counts as "outside" the topmost dialog → pulled into it.
    const a1 = screen.getByRole('button', { name: 'a1' });
    a1.focus();
    fireEvent.keyDown(a1, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'b1' })).toHaveFocus();
  });

  it('leaves Tab alone when focus is inside a foreign overlay dialog (tour bubble)', () => {
    render(
      <>
        <div role="dialog">
          <button>tour-next</button>
        </div>
        <Modal isOpen onClose={vi.fn()}>
          <button>inside</button>
        </Modal>
      </>,
    );
    const next = screen.getByRole('button', { name: 'tour-next' });
    next.focus();
    // Foreign dialog (no data-modal) owns its Tab: no preventDefault, focus not yanked.
    expect(fireEvent.keyDown(next, { key: 'Tab' })).toBe(true);
    expect(next).toHaveFocus();
  });

  it('restores focus to the opener element on close', () => {
    const Wrapper = ({ open }: { open: boolean }) => (
      <>
        <button>trigger</button>
        <Modal isOpen={open} onClose={vi.fn()}>
          <button>inside</button>
        </Modal>
      </>
    );
    const { rerender } = render(<Wrapper open={false} />);
    const trigger = screen.getByRole('button', { name: 'trigger' });
    trigger.focus();
    rerender(<Wrapper open />);
    screen.getByRole('button', { name: 'inside' }).focus();
    rerender(<Wrapper open={false} />);
    expect(trigger).toHaveFocus();
  });
});
