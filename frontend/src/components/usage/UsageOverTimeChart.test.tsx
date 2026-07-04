import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { UsageOverTimeChart } from './UsageOverTimeChart';
import { formatTimeseriesLabel } from '@/lib/usageFormat';

// recharts needs real layout measurements; the chart internals are not under test —
// same mock as the page tests, so the data mapping and card chrome still execute.
// ComposedChart additionally captures its `data` prop so the point mapping
// (label + total_tokens) is asserted, not just that the card renders.
let capturedChartData: Array<Record<string, unknown>> = [];
vi.mock('recharts', async (importOriginal) => {
  const mod = await importOriginal<typeof import('recharts')>();
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    ComposedChart: (props: { data: Array<Record<string, unknown>>; children: React.ReactNode }) => {
      capturedChartData = props.data;
      return <div>{props.children}</div>;
    },
  };
});

const POINTS = [
  {
    timestamp: '2026-07-01T00:00:00Z',
    input_tokens: 1000,
    output_tokens: 500,
    cache_read_tokens: 8000,
    cache_write_tokens: 2000,
    cost_usd: 0.5,
    request_count: 3,
  },
  {
    timestamp: '2026-07-02T00:00:00Z',
    input_tokens: 2000,
    output_tokens: 700,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cost_usd: 0.7,
    request_count: 1,
  },
];

describe('UsageOverTimeChart', () => {
  it('renders nothing for an empty window', () => {
    const { container } = render(<UsageOverTimeChart timeseries={[]} period="monthly" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the card and maps the points (daily labels for non-hourly periods)', () => {
    render(<UsageOverTimeChart timeseries={POINTS} period="monthly" />);
    expect(screen.getByText('Usage Over Time')).toBeInTheDocument();
    // total_tokens = fresh input + output, cache EXCLUDED (must keep matching the
    // "Total Tokens" tile); label = ISO date on long windows.
    expect(capturedChartData[0]).toMatchObject({ total_tokens: 1500, label: '2026-07-01' });
    expect(capturedChartData[1]).toMatchObject({ total_tokens: 2700, label: '2026-07-02' });
  });

  it('renders for hourly periods too (time-of-day label path)', () => {
    render(<UsageOverTimeChart timeseries={POINTS} period="hourly" />);
    expect(screen.getByText('Usage Over Time')).toBeInTheDocument();
    // Time-of-day label, not the ISO date (exact rendering is locale/TZ-dependent).
    expect(capturedChartData[0].label).not.toBe('2026-07-01');
    expect(String(capturedChartData[0].label)).toMatch(/\d{1,2}:\d{2}/);
  });

  it('formatTimeseriesLabel branches on the period', () => {
    expect(formatTimeseriesLabel('2026-07-01T13:45:00Z', 'monthly')).toBe('2026-07-01');
    expect(formatTimeseriesLabel('2026-07-01T13:45:00Z', 'weekly')).toBe('2026-07-01');
    expect(formatTimeseriesLabel('2026-07-01T13:45:00Z', 'hourly')).toMatch(/\d{1,2}:\d{2}/);
    expect(formatTimeseriesLabel('2026-07-01T13:45:00Z', 'daily')).toMatch(/\d{1,2}:\d{2}/);
  });
});
