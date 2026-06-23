import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAwsEnumProgress } from './useAwsEnumProgress';

vi.mock('@/services/api', () => ({ getAwsEnumProgress: vi.fn() }));
import { getAwsEnumProgress } from '@/services/api';
const mockProgress = vi.mocked(getAwsEnumProgress);

describe('useAwsEnumProgress', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockProgress.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reports the active phase, then null once the backend goes idle', async () => {
    mockProgress
      .mockResolvedValueOnce({ phase: 'discovering', done: 1, total: 3 })
      .mockResolvedValue({ phase: 'idle', done: 0, total: 0 });

    const { result } = renderHook(() => useAwsEnumProgress('s1', true));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // first (immediate) poll
    });
    expect(result.current).toEqual({ phase: 'discovering', done: 1, total: 3 });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // next poll → idle
    });
    expect(result.current).toBeNull();
  });

  it('stays null and does not poll while inactive', async () => {
    const { result } = renderHook(() => useAwsEnumProgress('s1', false));
    await vi.advanceTimersByTimeAsync(2000);
    expect(result.current).toBeNull();
    expect(mockProgress).not.toHaveBeenCalled();
  });
});
