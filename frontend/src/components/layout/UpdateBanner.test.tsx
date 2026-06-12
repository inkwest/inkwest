import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { UpdateBanner } from './UpdateBanner';
import { useUpdateStore } from '@/store/update';

describe('UpdateBanner', () => {
  beforeEach(() => {
    useUpdateStore.getState().reset();
  });

  it('renders nothing while no update is available', () => {
    const { container } = render(<UpdateBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('shows the banner once an update is available', () => {
    useUpdateStore.setState({ updateAvailable: true });
    render(<UpdateBanner />);
    expect(screen.getByText('A new version of Inkwest is available.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
  });

  it('dismiss hides the banner without forcing a reload', () => {
    useUpdateStore.setState({ updateAvailable: true });
    render(<UpdateBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(useUpdateStore.getState().updateAvailable).toBe(false);
  });

  describe('reload', () => {
    const original = window.location;
    beforeEach(() => {
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...original, reload: vi.fn() },
      });
    });
    afterEach(() => {
      Object.defineProperty(window, 'location', { configurable: true, value: original });
    });

    it('reloads the page on Reload', () => {
      useUpdateStore.setState({ updateAvailable: true });
      render(<UpdateBanner />);
      fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
      expect(window.location.reload).toHaveBeenCalledOnce();
    });
  });
});
