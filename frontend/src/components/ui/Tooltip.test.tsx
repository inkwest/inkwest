import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Tooltip } from './Tooltip';

describe('Tooltip', () => {
  it('shows the content on hover and hides it on leave', () => {
    render(
      <Tooltip content="explain me">
        <span>trigger</span>
      </Tooltip>,
    );
    const trigger = screen.getByText('trigger');

    // Hidden until hovered.
    expect(screen.queryByRole('tooltip')).toBeNull();

    fireEvent.mouseMove(trigger, { clientX: 10, clientY: 10 });
    expect(screen.getByRole('tooltip')).toHaveTextContent('explain me');

    fireEvent.mouseLeave(trigger.parentElement as HTMLElement);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('exposes a string content as the trigger aria-label', () => {
    render(
      <Tooltip content="accessible label">
        <span>x</span>
      </Tooltip>,
    );
    expect(screen.getByLabelText('accessible label')).toBeTruthy();
  });
});
