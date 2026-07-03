import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ToggleSwitch } from './ToggleSwitch';

describe('ToggleSwitch', () => {
  it('renders as an accessible switch reflecting checked state', () => {
    const { rerender } = render(<ToggleSwitch checked={false} onChange={() => {}} aria-label="t" />);
    expect(screen.getByRole('switch', { name: 't' })).not.toBeChecked();
    rerender(<ToggleSwitch checked onChange={() => {}} aria-label="t" />);
    expect(screen.getByRole('switch', { name: 't' })).toBeChecked();
  });

  it('fires onChange with the flipped value; onClick runs first but cannot cancel it', () => {
    const onChange = vi.fn();
    const onClick = vi.fn((e: React.MouseEvent) => e.stopPropagation());
    render(<ToggleSwitch checked={false} onChange={onChange} onClick={onClick} aria-label="t" />);
    fireEvent.click(screen.getByRole('switch'));
    expect(onClick).toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('does not toggle when disabled', () => {
    const onChange = vi.fn();
    render(<ToggleSwitch checked={false} onChange={onChange} disabled aria-label="t" />);
    fireEvent.click(screen.getByRole('switch'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
