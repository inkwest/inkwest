import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { openMcpAuthPopup } from './mcpConnect';
import { startMcpAuth } from '@/services/api';

vi.mock('@/services/api', () => ({ startMcpAuth: vi.fn() }));

const mockStart = vi.mocked(startMcpAuth);

describe('openMcpAuthPopup', () => {
  let popup: { location: { href: string }; close: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    popup = { location: { href: 'about:blank' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    // jsdom has no real screen geometry; the centring math only needs numbers.
    vi.spyOn(window, 'screen', 'get').mockReturnValue({ width: 1920, height: 1080 } as Screen);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('navigates the popup to the authorize URL on success', async () => {
    mockStart.mockResolvedValue({ authorize_url: 'https://as.example.com/authorize?x=1' });
    openMcpAuthPopup('example-server');
    await vi.waitFor(() => expect(popup.location.href).toBe('https://as.example.com/authorize?x=1'));
    expect(popup.close).not.toHaveBeenCalled();
  });

  it('closes the popup and surfaces the backend error detail on failure', async () => {
    // ApiError extends Error; its message is the backend 502 detail (e.g. discovery failure).
    mockStart.mockRejectedValue(new Error('AS metadata issuer mismatch'));
    const onError = vi.fn();
    openMcpAuthPopup('example-server', onError);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith('AS metadata issuer mismatch'));
    expect(popup.close).toHaveBeenCalled();
  });

  it('falls back to a generic message for a non-Error throw', async () => {
    mockStart.mockRejectedValue('boom');
    const onError = vi.fn();
    openMcpAuthPopup('example-server', onError);
    await vi.waitFor(() =>
      expect(onError).toHaveBeenCalledWith('Failed to start MCP authorization'),
    );
  });
});
