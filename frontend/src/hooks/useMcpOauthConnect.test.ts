import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { McpServerEntry } from '@/services/api';

vi.mock('@/lib/mcpConnect', () => ({ openMcpAuthPopup: vi.fn() }));
vi.mock('@/components/connector/mcpCatalogCache', () => ({ loadMcpCatalog: vi.fn() }));

import { useMcpOauthConnect } from './useMcpOauthConnect';
import { openMcpAuthPopup } from '@/lib/mcpConnect';
import { loadMcpCatalog } from '@/components/connector/mcpCatalogCache';

const mockOpen = vi.mocked(openMcpAuthPopup);
const mockLoad = vi.mocked(loadMcpCatalog);

function catalog(status: McpServerEntry['auth_status']) {
  return {
    keys: [] as string[],
    servers: [{ key: 'notion', kind: 'oauth2' as const, auth_status: status }],
  };
}

describe('useMcpOauthConnect', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fires onConnected(servers, key) once the poll observes a connected grant', async () => {
    mockOpen.mockReturnValue({ closed: false, close: vi.fn() } as unknown as Window);
    mockLoad.mockResolvedValue(catalog('not_connected'));
    const onConnected = vi.fn();
    const { result } = renderHook(() => useMcpOauthConnect(onConnected));

    act(() => result.current.connect('notion'));
    await act(() => vi.advanceTimersByTimeAsync(2000)); // still not connected
    expect(onConnected).not.toHaveBeenCalled();

    mockLoad.mockResolvedValue(catalog('connected'));
    await act(() => vi.advanceTimersByTimeAsync(2000)); // now connected
    expect(onConnected).toHaveBeenCalledWith(catalog('connected').servers, 'notion');
  });

  it('keeps polling when the popup is blocked (null) — a blocked popup is not an abandon', async () => {
    mockOpen.mockReturnValue(null); // popup blocker returned nothing
    mockLoad.mockResolvedValue(catalog('not_connected'));
    const onConnected = vi.fn();
    const { result } = renderHook(() => useMcpOauthConnect(onConnected));

    act(() => result.current.connect('notion'));
    // Old behaviour stopped on the first tick (!popup); it must now keep polling instead.
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(onConnected).not.toHaveBeenCalled();

    // The grant lands out-of-band → a later tick still observes it despite the null popup.
    mockLoad.mockResolvedValue(catalog('connected'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(onConnected).toHaveBeenCalledWith(catalog('connected').servers, 'notion');
  });

  it('stops polling when the user closes the popup (abandon)', async () => {
    const popup = { closed: false, close: vi.fn() };
    mockOpen.mockReturnValue(popup as unknown as Window);
    mockLoad.mockResolvedValue(catalog('not_connected'));
    const onConnected = vi.fn();
    const { result } = renderHook(() => useMcpOauthConnect(onConnected));

    act(() => result.current.connect('notion'));
    popup.closed = true; // user closed the consent window
    await act(() => vi.advanceTimersByTimeAsync(2000));
    // A subsequent connect is possible again (the key is no longer in-flight).
    mockLoad.mockResolvedValue(catalog('connected'));
    await act(() => vi.advanceTimersByTimeAsync(2000)); // no live poll → nothing observes it
    expect(onConnected).not.toHaveBeenCalled();
  });

  it('drops a second connect for the same key while a poll is in flight', async () => {
    mockOpen.mockReturnValue({ closed: false, close: vi.fn() } as unknown as Window);
    mockLoad.mockResolvedValue(catalog('not_connected'));
    const { result } = renderHook(() => useMcpOauthConnect());

    act(() => result.current.connect('notion'));
    act(() => result.current.connect('notion')); // in-flight guard drops this one
    expect(mockOpen).toHaveBeenCalledTimes(1);
  });
});
