import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { ResizableDivider } from './ResizableDivider';

describe('ResizableDivider', () => {
  afterEach(() => {
    cleanup();
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });

  it('reports the horizontal delta only while a drag is live', () => {
    const onResize = vi.fn();
    render(<ResizableDivider onResize={onResize} />);
    const handle = screen.getByRole('separator');

    // No drag started yet → moves are ignored.
    fireEvent.mouseMove(window, { clientX: 500 });
    expect(onResize).not.toHaveBeenCalled();

    fireEvent.mouseDown(handle, { clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 130 });
    expect(onResize).toHaveBeenLastCalledWith(30); // delta from the mousedown anchor
    fireEvent.mouseMove(window, { clientX: 120 });
    expect(onResize).toHaveBeenLastCalledWith(-10); // delta from the previous move

    // Release → subsequent moves are ignored again.
    fireEvent.mouseUp(window);
    onResize.mockClear();
    fireEvent.mouseMove(window, { clientX: 300 });
    expect(onResize).not.toHaveBeenCalled();
  });

  it('resets on double-click', () => {
    const onReset = vi.fn();
    render(<ResizableDivider onResize={vi.fn()} onReset={onReset} />);
    fireEvent.doubleClick(screen.getByRole('separator'));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it('locks the body cursor during a drag and releases it on drop', () => {
    render(<ResizableDivider onResize={vi.fn()} />);
    const handle = screen.getByRole('separator');
    fireEvent.mouseDown(handle, { clientX: 0 });
    expect(document.body.style.cursor).toBe('col-resize');
    fireEvent.mouseUp(window);
    expect(document.body.style.cursor).toBe('');
  });

  it('releases the drag on window blur (mouseup missed outside the window)', () => {
    const onResize = vi.fn();
    render(<ResizableDivider onResize={onResize} />);
    const handle = screen.getByRole('separator');
    fireEvent.mouseDown(handle, { clientX: 0 });
    fireEvent.blur(window); // focus left the window while dragging → safety release
    expect(document.body.style.cursor).toBe('');
    // Drag is no longer live: subsequent moves are ignored.
    fireEvent.mouseMove(window, { clientX: 200 });
    expect(onResize).not.toHaveBeenCalled();
  });
});
