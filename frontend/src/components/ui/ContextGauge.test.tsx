import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ContextGauge } from './ContextGauge';
import { GAUGE_ALERT_PERCENT, GAUGE_WARN_PERCENT, type ContextUsage } from '@/lib/contextUsage';

function usageAt(percent: number): ContextUsage {
  const window = 200_000;
  return { used: Math.round((percent / 100) * window), window, percent };
}

/** The bar's fill span, whose class carries the tone. */
function fill(container: HTMLElement): HTMLElement {
  const el = container.querySelector('span[aria-hidden="true"] > span');
  if (!el) throw new Error('gauge fill not rendered');
  return el as HTMLElement;
}

describe('ContextGauge', () => {
  it('stays neutral below the warn threshold', () => {
    const { container } = render(<ContextGauge usage={usageAt(GAUGE_WARN_PERCENT - 25)} />);
    expect(screen.getByText('50%')).toHaveClass('text-gray-500');
    expect(fill(container)).toHaveClass('bg-gray-500');
  });

  it('turns amber at the warn threshold', () => {
    const { container } = render(<ContextGauge usage={usageAt(GAUGE_WARN_PERCENT)} />);
    expect(screen.getByText(`${GAUGE_WARN_PERCENT}%`)).toHaveClass('text-amber-400');
    expect(fill(container)).toHaveClass('bg-amber-500');
  });

  it('turns red at the alert threshold and says what to do', () => {
    const { container } = render(<ContextGauge usage={usageAt(GAUGE_ALERT_PERCENT)} />);
    expect(screen.getByText(`${GAUGE_ALERT_PERCENT}%`)).toHaveClass('text-red-400');
    expect(fill(container)).toHaveClass('bg-red-500');
    expect(screen.getByRole('img')).toHaveAccessibleName(/compact the conversation/i);
  });

  it('reports the exact token counts, not just the percentage', () => {
    render(<ContextGauge usage={{ used: 128_000, window: 200_000, percent: 64 }} />);
    expect(screen.getByRole('img')).toHaveAccessibleName(
      /128,000 of 200,000 tokens \(64%\) on the last turn/i
    );
  });

  it('rounds the width to the reported percentage without a floor', () => {
    const { container } = render(<ContextGauge usage={{ used: 400, window: 200_000, percent: 0.2 }} />);
    expect(fill(container).style.width).toBe('0%');
  });
});
