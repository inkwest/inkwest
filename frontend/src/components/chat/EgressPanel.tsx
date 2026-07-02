import { useMemo, useState } from 'react';
import { Globe, ShieldCheck, ShieldAlert, AlertTriangle } from 'lucide-react';
import { EgressEvent, isBlocked, isError } from '@/hooks/useEgressSocket';
import { Tooltip } from '@/components/ui';
import { cn } from '@/lib/utils';

// The three outcome categories an egress event falls into — the same classification
// the row icons/colors use (see isBlocked/isError). Used to filter the live log.
type EgressKind = 'allowed' | 'blocked' | 'error';
type EgressFilter = 'all' | EgressKind;

function kindOf(e: EgressEvent): EgressKind {
  if (isBlocked(e)) return 'blocked';
  if (isError(e)) return 'error';
  return 'allowed';
}

// Render squid timestamps in the viewer's own timezone/locale (no hardcoded zone) so
// the time reads correctly wherever the operator is — the underlying instant is the
// absolute epoch from the log. Forced 24h for an unambiguous audit format. Built once.
const TIME_FMT = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function EgressRow({ e }: { e: EgressEvent }) {
  const blocked = isBlocked(e);
  const errored = !blocked && isError(e);
  // ts is squid's epoch-seconds (float, ms precision); 0 means it failed to parse.
  const at = e.ts ? new Date(e.ts * 1000) : null;
  // Tooltip on the whole row so the explanation surfaces wherever the cursor lands.
  const detail = blocked
    ? 'Blocked by the squid proxy — host outside the egress allowlist'
    : errored
      ? 'Upstream error — squid forwarded the request; the destination (or the connection to it) failed'
      : 'Allowed through the egress proxy';
  return (
    <Tooltip
      content={detail}
      className={cn(
        'flex items-center gap-2 py-1 px-2 rounded text-xs',
        blocked && 'bg-red-900/20',
        errored && 'bg-amber-900/20'
      )}
    >
      {at && (
        <span className="shrink-0 font-mono text-[11px] text-gray-500 tabular-nums">
          {TIME_FMT.format(at)}
        </span>
      )}
      {blocked ? (
        // Squid policy block: the ACL refused this host.
        <ShieldAlert className="h-3.5 w-3.5 text-red-400 shrink-0" />
      ) : errored ? (
        // Upstream/connection error (e.g. TCP_TUNNEL/503) — not a block.
        <AlertTriangle className="h-3.5 w-3.5 text-amber-400 shrink-0" />
      ) : (
        <ShieldCheck className="h-3.5 w-3.5 text-green-400 shrink-0" />
      )}
      <span className="flex-1 min-w-0 truncate font-mono text-gray-200">
        {e.host}{e.port ? `:${e.port}` : ''}
      </span>
      {/* Fixed-width, left-aligned status/method cells so their left edges line up
          into clean columns across rows; the cell is always present (empty for an
          allowed event) so the method column never shifts. */}
      <span
        className={cn(
          'w-40 shrink-0 text-left text-[11px] font-mono',
          blocked ? 'text-red-400' : 'text-amber-400'
        )}
      >
        {(blocked || errored) && `${e.status} · ${blocked ? 'proxy block' : 'upstream error'}`}
      </span>
      <span
        className={cn(
          'w-20 shrink-0 text-left text-[11px] font-mono',
          blocked ? 'text-red-400' : errored ? 'text-amber-400' : 'text-gray-500'
        )}
      >
        {e.method}
      </span>
    </Tooltip>
  );
}

const FILTERS: { value: EgressFilter; label: string; activeClass: string }[] = [
  { value: 'all', label: 'All', activeClass: 'bg-gray-700 text-gray-100' },
  { value: 'allowed', label: 'Allowed', activeClass: 'bg-green-900/50 text-green-300' },
  { value: 'blocked', label: 'Blocked', activeClass: 'bg-red-900/50 text-red-300' },
  { value: 'error', label: 'Errors', activeClass: 'bg-amber-900/50 text-amber-300' },
];

/**
 * Full-pane live network-egress audit for a conversation: every host the sandboxed
 * agent reached through its squid proxy, classified by outcome — allowed (green),
 * blocked by the egress policy (red), or an upstream/connection error that squid
 * forwarded but the destination failed (amber). Rendered inside the "Network" tab.
 * A filter bar narrows the log to one outcome category.
 */
export function EgressLog({ events, connected }: { events: EgressEvent[]; connected: boolean }) {
  const [filter, setFilter] = useState<EgressFilter>('all');

  // Per-category counts label the filter chips; recomputed only when events change.
  const counts = useMemo(() => {
    const c = { all: events.length, allowed: 0, blocked: 0, error: 0 };
    for (const e of events) c[kindOf(e)]++;
    return c;
  }, [events]);

  const shown = filter === 'all' ? events : events.filter((e) => kindOf(e) === filter);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {events.length > 0 && (
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-gray-800 shrink-0">
          {FILTERS.map(({ value, label, activeClass }) => (
            <button
              key={value}
              onClick={() => setFilter(value)}
              className={cn(
                'text-[11px] px-2 py-0.5 rounded-full transition-colors',
                filter === value ? activeClass : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800',
              )}
            >
              {label}
              <span className="ml-1 opacity-70">{counts[value]}</span>
            </button>
          ))}
        </div>
      )}
      <div className="flex-1 overflow-y-auto p-3 space-y-0.5 min-h-0">
        {events.length === 0 ? (
          <div className="flex items-center gap-2 p-2 text-sm text-gray-500">
            <Globe className="h-4 w-4 shrink-0" />
            {connected
              ? 'No egress yet — hosts the agent reaches appear here (HTTPS shows on connection close).'
              : 'Connecting to the egress stream…'}
          </div>
        ) : shown.length === 0 ? (
          <div className="flex items-center gap-2 p-2 text-sm text-gray-500">
            <Globe className="h-4 w-4 shrink-0" />
            No {filter} events.
          </div>
        ) : (
          shown.map((e) => <EgressRow key={e._id} e={e} />)
        )}
      </div>
    </div>
  );
}
