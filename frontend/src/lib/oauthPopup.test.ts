import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from 'vitest';
import { openAuthPopup } from './oauthPopup';

describe('openAuthPopup', () => {
  let popup: { location: { href: string }; close: ReturnType<typeof vi.fn> };
  let openSpy: MockInstance;

  beforeEach(() => {
    popup = { location: { href: 'about:blank' }, close: vi.fn() };
    openSpy = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens a blank popup synchronously, then navigates it to the resolved URL', async () => {
    const win = openAuthPopup('x-oauth', { width: 600, height: 720 }, async () => 'https://as/authorize');
    // Opened straight away (before the async resolve) so popup blockers don't trip.
    expect(openSpy).toHaveBeenCalledWith('about:blank', 'x-oauth', expect.stringContaining('width=600'));
    expect(win).toBe(popup);
    await vi.waitFor(() => expect(popup.location.href).toBe('https://as/authorize'));
    expect(popup.close).not.toHaveBeenCalled();
  });

  it('closes the popup and reports the error when the URL cannot be resolved', async () => {
    const onError = vi.fn();
    const boom = new Error('discovery failed');
    openAuthPopup('x-oauth', { width: 600, height: 720 }, async () => { throw boom; }, onError);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(boom));
    expect(popup.close).toHaveBeenCalled();
  });

  it('does not throw when window.open is blocked (null popup)', async () => {
    openSpy.mockReturnValue(null);
    const onError = vi.fn();
    const win = openAuthPopup('x-oauth', { width: 600, height: 720 }, async () => 'https://as/authorize', onError);
    expect(win).toBeNull();
    // Resolve path must tolerate a null popup (no navigation, no throw).
    await new Promise((r) => setTimeout(r, 0));
    expect(onError).not.toHaveBeenCalled();
  });
});
