import { cn } from '@/lib/utils';
import {
  GAUGE_ALERT_PERCENT,
  GAUGE_WARN_PERCENT,
  type ContextUsage,
} from '@/lib/contextUsage';

/**
 * Context-window occupancy as a slim bar + percentage, next to the header token glyphs.
 * Distinct from those glyphs: they count cumulative session spend, this shows how full the
 * *current* prompt is — the number that says whether the conversation is about to lose its
 * beginning.
 */
export function ContextGauge({
  usage,
  className,
}: {
  usage: ContextUsage;
  className?: string;
}) {
  const percent = Math.round(usage.percent);
  const alert = percent >= GAUGE_ALERT_PERCENT;
  const warn = !alert && percent >= GAUGE_WARN_PERCENT;
  const title =
    `Context: ${usage.used.toLocaleString()} of ${usage.window.toLocaleString()} tokens ` +
    `(${percent}%) on the last turn` +
    (alert ? ' — near the model limit, compact the conversation' : '');

  return (
    <span
      className={cn('inline-flex items-center gap-1.5', className)}
      role="img"
      title={title}
      aria-label={title}
    >
      <span className="h-1.5 w-10 rounded-full bg-gray-700 overflow-hidden" aria-hidden="true">
        <span
          className={cn(
            'block h-full rounded-full',
            alert ? 'bg-red-500' : warn ? 'bg-amber-500' : 'bg-gray-500'
          )}
          style={{ width: `${percent}%` }}
        />
      </span>
      <span className={cn(alert ? 'text-red-400' : warn ? 'text-amber-400' : 'text-gray-500')}>
        {percent}%
      </span>
    </span>
  );
}
