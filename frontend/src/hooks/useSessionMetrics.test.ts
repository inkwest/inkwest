import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useSessionMetrics, MetricsSnapshot } from './useSessionMetrics';

vi.mock('@/services/api', () => ({ fetchAPI: vi.fn() }));
import { fetchAPI } from '@/services/api';

const mockFetch = fetchAPI as unknown as ReturnType<typeof vi.fn>;

const runningSnapshot: MetricsSnapshot = {
  ts: 1,
  status: 'running',
  conv: {
    cpu_pct: 5,
    mem_bytes: 1024,
    mem_limit_bytes: 2048,
    net_rx_bytes: 0,
    net_tx_bytes: 0,
    blk_read_bytes: 0,
    blk_write_bytes: 0,
  },
  proxy: null,
  disk_bytes: 42,
  disk_total_bytes: 1_000_000,
};

beforeEach(() => {
  mockFetch.mockReset();
});

describe('useSessionMetrics', () => {
  it('does nothing without a session id', () => {
    const { result } = renderHook(() => useSessionMetrics(undefined));
    expect(result.current.samples).toHaveLength(0);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('accumulates running snapshots stamped with a client time', async () => {
    mockFetch.mockResolvedValue(runningSnapshot);
    const { result } = renderHook(() => useSessionMetrics('s1'));

    await waitFor(() => expect(result.current.status).toBe('running'));
    expect(mockFetch).toHaveBeenCalledWith('/api/sessions/s1/metrics');
    expect(result.current.samples.length).toBeGreaterThan(0);
    expect(result.current.samples[0].conv?.cpu_pct).toBe(5);
    expect(typeof result.current.samples[0].t).toBe('number');
  });

  it('tracks status but keeps no samples when not running', async () => {
    mockFetch.mockResolvedValue({
      ts: 1,
      status: 'not_running',
      conv: null,
      proxy: null,
      disk_bytes: null,
      disk_total_bytes: null,
    } satisfies MetricsSnapshot);
    const { result } = renderHook(() => useSessionMetrics('s1'));

    await waitFor(() => expect(result.current.status).toBe('not_running'));
    expect(result.current.samples).toHaveLength(0);
  });

  it('reports the disabled status when the sandbox feature is off', async () => {
    mockFetch.mockResolvedValue({
      ts: 1,
      status: 'disabled',
      conv: null,
      proxy: null,
      disk_bytes: null,
      disk_total_bytes: null,
    } satisfies MetricsSnapshot);
    const { result } = renderHook(() => useSessionMetrics('s1'));

    await waitFor(() => expect(result.current.status).toBe('disabled'));
    expect(result.current.samples).toHaveLength(0);
  });
});
